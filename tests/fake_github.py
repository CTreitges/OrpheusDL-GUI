"""A tiny HTTP server on 127.0.0.1 that answers like GitHub's release API.

Routes are a dict ``path -> (status, headers, body)``; ``body`` may be bytes
or a callable returning bytes. Used by the update tests; also the base of the
manual test server in the run's work folder.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = "CTreitges/OrpheusDL-GUI"


class FakeGitHub:
    def __init__(self):
        self.routes = {}
        self.hits = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server API)
                server.hits.append(self.path)
                status, headers, body = server.routes.get(self.path, (404, {}, b"not found"))
                if callable(body):
                    body = body()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()

    # -- helpers --------------------------------------------------------------
    @property
    def api_path(self):
        return f"/repos/{REPO}/releases/latest"

    def asset_path(self, tag, name="OrpheusDL-GUI-windows.zip"):
        return f"/{REPO}/releases/download/{tag}/{name}"

    def publish(self, tag, payload, *, digest=None, name="OrpheusDL-GUI-windows.zip", extra=None):
        """Serve ``payload`` as the release asset of ``tag`` and the API answer for it."""
        digest = digest if digest is not None else "sha256:" + hashlib.sha256(payload).hexdigest()
        asset_path = self.asset_path(tag, name)
        self.routes[asset_path] = (200, {"Content-Type": "application/zip"}, payload)
        release = {
            "tag_name": tag,
            "name": tag,
            "body": f"Changes in {tag}",
            "draft": False,
            "prerelease": False,
            "html_url": f"{self.base}/{REPO}/releases/tag/{tag}",
            "assets": [
                {
                    "name": name,
                    "size": len(payload),
                    "digest": digest,
                    "browser_download_url": self.base + asset_path,
                }
            ],
        }
        if extra:
            release.update(extra)
        self.routes[self.api_path] = (
            200,
            {"Content-Type": "application/json"},
            json.dumps(release).encode("utf-8"),
        )
        return release
