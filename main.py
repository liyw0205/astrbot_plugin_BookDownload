"""AstrBot BookDownload plugin entry point.

The plugin currently provides the loadable foundation for book-download
features. Download providers and commands can be added without changing the
plugin metadata or package layout.
"""

from astrbot.api.star import Context, Star


class BookDownloadPlugin(Star):
    """BookDownload plugin lifecycle entry point."""

    def __init__(self, context: Context):
        super().__init__(context)

