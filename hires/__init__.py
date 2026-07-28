"""Hi-Res suite for OrpheusDL-GUI.

Adds three things on top of the stock GUI:

* a persistent, reorderable download queue (``queue_store``)
* a TIDAL playlist browser (``tidal_library``)
* Spotify -> TIDAL playlist conversion with hi-res download (``spotify_source``,
  ``matcher``, ``converter``)

Everything outside of ``gui_panel`` is deliberately tkinter-free so it can be
unit tested headlessly.
"""

__version__ = "1.0.0"
