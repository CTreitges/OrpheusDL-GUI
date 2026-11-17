"""UI logic of the Port tab: read a playlist on one service, match on another.

Built on :class:`~hires.controllers.SpotifyImportController`, which already
does everything a port needs -- superseding runs, batch conversion, the review
step. What this adds is the choice of direction: any of the 12 pairs of the
four services. Spotify -> TIDAL is the default and behaves as the old tab did.

Porting only reads from the source and only searches on the target. Nothing
is ever written to an account; the result is a group of queue items.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from .controllers import SpotifyImportController, UiDispatcher, _describe, run_in_background
from .converter import PlaylistConverter
from .matcher import TrackMatcher
from .models import ConversionReport, FolderRef, HiresError, PlaylistRef, QueueItem, flat_root
from .playlist_providers import SERVICES, TARGET_QUALITY, ProviderStatus, port_directions
from .playlists_controller import provider_status

MatcherFactory = Callable[[Any, str], Any]


def _default_matcher(target: Any, target_name: str) -> TrackMatcher:
    return TrackMatcher(target, target_name=target_name)


class PortController(SpotifyImportController):
    """Ports playlists between any two of the four services."""

    def __init__(
        self,
        providers: Dict[str, Callable[[], Any]],
        queue,
        *,
        spotify_source_provider: Callable[[], Any],
        dispatch: Optional[UiDispatcher] = None,
        output_provider: Optional[Callable[[], Optional[str]]] = None,
        matcher_factory: Optional[MatcherFactory] = None,
        open_url: Optional[Callable[[str], Any]] = None,
        wait_for_code: Optional[Callable[..., str]] = None,
    ):
        """
        Args:
            providers: service name -> provider factory (see provider_registry).
            spotify_source_provider: the raw Spotify source. Kept as the
                parent's ``source_provider`` so the Spotify sign-in -- here and
                on the Accounts tab, which shares this controller -- works
                whatever direction is selected.
            matcher_factory: ``(target_provider, target_name) -> matcher``;
                injectable for tests.
        """
        super().__init__(
            spotify_source_provider,
            lambda: None,  # unused: _build_converter picks the target itself
            queue,
            dispatch=dispatch,
            output_provider=output_provider,
            open_url=open_url,
            wait_for_code=wait_for_code,
        )
        self.providers = dict(providers)
        self.matcher_factory = matcher_factory or _default_matcher
        #: (report, source ids already queued from it) -- the latest report only.
        self._queued: Optional[Tuple[ConversionReport, set]] = None
        self.source_service = "Spotify"
        self.target_service = "TIDAL"

    # -- direction ----------------------------------------------------------
    @property
    def direction(self) -> Tuple[str, str]:
        return self.source_service, self.target_service

    def directions(self) -> List[Tuple[str, str]]:
        offered = [s for s in SERVICES if s in self.providers]
        return port_directions(offered)

    def set_direction(self, source: str, target: str) -> None:
        if (source, target) not in self.directions():
            raise HiresError(f"Cannot port from {source} to {target}.")
        if (source, target) != self.direction:
            # A run for the old direction must not finish into the new one.
            self._begin_run()
            self.report = None
        self.source_service, self.target_service = source, target

    def status(self, service: str) -> ProviderStatus:
        try:
            return provider_status(self.providers[service](), service)
        except Exception as exc:
            return ProviderStatus(service, available=False, can_search=False, hint=_describe(exc))

    def read_statuses(self, on_done: Callable[[ProviderStatus, ProviderStatus], None]) -> threading.Thread:
        """Source and target status off the main thread (loading a module can take a while)."""
        source, target = self.direction

        def work():
            self.dispatch(on_done, self.status(source), self.status(target))

        return run_in_background(work, name="hires-port-status")

    # -- converter ----------------------------------------------------------
    def _build_converter(self, direction: Optional[Tuple[str, str]] = None) -> PlaylistConverter:
        source_name, target_name = direction or self.direction
        source = self.providers[source_name]()
        target = self.providers[target_name]()
        source_status = provider_status(source, source_name)
        if not source_status.available:
            raise HiresError(source_status.hint)
        target_status = provider_status(target, target_name)
        if not target_status.available or not target_status.can_search:
            raise HiresError(target_status.hint or f"{target_name} cannot be searched.")
        return PlaylistConverter(
            source,
            self.matcher_factory(target, target_name),
            self.queue,
            quality=getattr(target, "quality", None),
            output_root=self.output_provider(),
            source_name=source_name,
            target_name=target_name,
        )

    def _converter_for(self, report: ConversionReport) -> PlaylistConverter:
        """Queue a report in the direction that produced it.

        Queueing needs neither service: no provider is built (building the
        TIDAL one can load the module, and this runs on the UI thread).
        """
        return PlaylistConverter(
            None,
            None,
            self.queue,
            quality=TARGET_QUALITY.get(report.target_service),
            output_root=self.output_provider(),
            source_name=report.source_service,
            target_name=report.target_service,
        )

    def apply_review_and_enqueue(
        self,
        decisions: Dict[str, Any],
        *,
        report: Optional[ConversionReport] = None,
        include_unreviewed: bool = False,
    ) -> List[QueueItem]:
        from .converter import apply_review

        report = report or self.report
        if report is None:
            return []
        apply_review(report, decisions)
        return self._enqueue_new(report, include_review=include_unreviewed)

    def enqueue_confident_only(self, report: Optional[ConversionReport] = None) -> List[QueueItem]:
        report = report or self.report
        if report is None:
            return []
        return self._enqueue_new(report, include_review=False)

    def _enqueue_new(self, report: ConversionReport, *, include_review: bool) -> List[QueueItem]:
        """Queue the accepted matches of ``report`` that are not queued yet.

        "Queue matches" (or "Port & queue") and then "Review uncertain…" both
        queue from the same report; without this the confident tracks were
        queued -- and downloaded -- a second time with the reviewed ones.
        Same playlist, same start time: the remainder joins the same group.
        """
        if self._queued is None or self._queued[0] is not report:
            self._queued = (report, set())
        done = self._queued[1]
        fresh = ConversionReport(
            playlist=report.playlist,
            results=[r for r in report.results if r.source.id not in done],
            started_at=report.started_at,
            finished_at=report.finished_at,
            source_service=report.source_service,
            target_service=report.target_service,
        )
        items = self._converter_for(report).enqueue(fresh, include_review=include_review)
        accepted = fresh.auto_accepted + (fresh.needs_review if include_review else [])
        done.update(r.source.id for r in accepted if r.best is not None)
        return items

    def convert_and_queue(self, playlist: Any, on_progress, on_done, on_error) -> threading.Thread:
        """*Port & queue*: port, then queue the confident matches at once.

        ``on_done(report, items)`` runs on the main thread like every other
        callback. Uncertain matches are not queued; they stay in the report
        for the review dialog. A run superseded by a newer one queues nothing,
        because :meth:`convert` drops its result.
        """

        def finished(report: ConversionReport) -> None:
            try:
                items = self.enqueue_confident_only(report)
            except Exception as exc:
                on_error(_describe(exc))
                return
            on_done(report, items)

        return self.convert(playlist, on_progress, finished, on_error)

    # -- listing ------------------------------------------------------------
    def load_playlists(self, on_done, on_error, on_status=None) -> threading.Thread:
        """The source's own playlists. Spotify keeps its sign-in-on-demand.

        An answer that arrives after the direction changed is dropped: shown
        under the new source, a click would port the wrong service's playlist.
        """
        direction = self.direction

        def still(callback):
            def deliver(*args):
                if self.direction == direction:
                    callback(*args)
            return deliver

        on_done, on_error = still(on_done), still(on_error)
        if self.source_service == "Spotify":
            return super().load_playlists(on_done, on_error, on_status=on_status)
        service = self.source_service

        def work():
            try:
                provider = self.providers[service]()
                root = provider.list_folders(include_favorites=True)
            except Exception as exc:
                self.dispatch(on_error, _describe(exc))
                return
            if self.direction == direction:
                self.playlists = root.all_playlists()
            self.dispatch(on_done, root)

        return run_in_background(work, name="hires-port-playlists")


def with_linked_playlist(root: Optional[FolderRef], playlist: PlaylistRef) -> FolderRef:
    """The Port list after porting ``playlist`` by link: it shows as a row.

    Put on top of what is listed (your own playlists, earlier links); a
    playlist already listed is not added twice.
    """
    root = root or flat_root([])
    if any(p.id == playlist.id for p in root.all_playlists()):
        return root
    return FolderRef(
        id=root.id,
        name=root.name,
        playlists=[playlist] + list(root.playlists),
        folders=list(root.folders),
        platform=root.platform,
    )
