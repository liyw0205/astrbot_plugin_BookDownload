from dataclasses import dataclass


@dataclass(frozen=True)
class SearchResult:
    source: str
    title: str
    url: str
    cover_url: str = ""
    author: str = ""
    page_count: int | None = None
    tags: tuple[str, ...] = ()
    similarity: float | None = None


@dataclass(frozen=True)
class GalleryDetail:
    """Metadata and preview pages needed by the detail card."""

    source: str
    gallery_id: str
    title: str
    url: str
    cover_url: str = ""
    tags: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    artists: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    page_count: int | None = None
    page_urls: tuple[str, ...] = ()
