from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from .models import SearchResult

NHENTAI_API = "https://nhentai.net/api/v2/search"
NHENTAI_THUMB_BASE = "https://t1.nhentai.net/"
EHENTAI_SEARCH = "https://e-hentai.org/"
EHENTAI_GALLERY_RE = re.compile(r"^/g/(\d+)/([a-zA-Z0-9]+)/?$")


def nhentai_result(gallery: dict[str, Any]) -> SearchResult | None:
    gallery_id = str(gallery.get("id", "")).strip()
    if not gallery_id.isdigit():
        return None

    title_data = gallery.get("title") or {}
    if isinstance(title_data, str):
        title = title_data
    elif isinstance(title_data, dict) and title_data:
        title = next(
            (str(title_data.get(key, "")).strip() for key in ("english", "pretty", "japanese") if title_data.get(key)),
            "(untitled)",
        )
    else:
        title = str(gallery.get("english_title") or gallery.get("japanese_title") or "(untitled)").strip()

    images = gallery.get("images") or {}
    cover = images.get("cover") or {}
    media_id = str(gallery.get("media_id", "")).strip()
    thumbnail = str(gallery.get("thumbnail") or "").strip()
    if thumbnail:
        cover_url = urljoin(NHENTAI_THUMB_BASE, thumbnail.lstrip("/"))
    elif media_id.isdigit():
        extension = str(cover.get("t", "j")).lower()
        extension = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}.get(extension, "jpg")
        cover_url = f"https://t.nhentai.net/galleries/{media_id}/cover.{extension}"
    else:
        cover_url = ""

    tags = tuple(
        str(tag.get("name", "")).strip()
        for tag in gallery.get("tags", [])
        if isinstance(tag, dict) and tag.get("name")
    )
    pages = gallery.get("num_pages")
    if not isinstance(pages, int):
        pages = len(images.get("pages", [])) or None
    return SearchResult(
        source="nhentai",
        title=title,
        url=f"https://nhentai.net/g/{gallery_id}/",
        cover_url=cover_url,
        page_count=pages,
        tags=tags,
    )


def parse_ehentai_results(html: str, base_url: str = EHENTAI_SEARCH) -> list[SearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[SearchResult] = []
    seen: set[str] = set()

    for anchor in soup.select("a[href]"):
        absolute_url = urljoin(base_url, anchor.get("href", ""))
        parsed_url = urlparse(absolute_url)
        if parsed_url.hostname not in {"e-hentai.org", "exhentai.org"}:
            continue
        if not EHENTAI_GALLERY_RE.match(parsed_url.path):
            continue
        canonical_url = f"https://{parsed_url.hostname}{parsed_url.path}"
        if canonical_url in seen:
            continue
        item = anchor.find_parent(class_="gl1t")
        if item is None:
            item = anchor.find_parent("tr") or anchor
        title_node = item.select_one(".glink")
        title = title_node.get_text(" ", strip=True) if title_node else anchor.get_text(" ", strip=True)
        if not title:
            continue
        image = item.select_one("img")
        cover = ""
        if image:
            cover = image.get("data-src") or image.get("src") or ""
            cover = urljoin(base_url, cover)
        pages = None
        page_text = item.get_text(" ", strip=True)
        page_match = re.search(r"(\d+)\s*pages?", page_text, re.IGNORECASE)
        if page_match:
            pages = int(page_match.group(1))
        results.append(
            SearchResult(
                source="ehentai",
                title=title,
                url=canonical_url,
                cover_url=cover,
                page_count=pages,
            )
        )
        seen.add(canonical_url)
    return results


def ehentai_search_url(query: str, page: int, base_url: str = EHENTAI_SEARCH) -> str:
    return f"{base_url}?{urlencode({'f_search': query, 'page': page - 1})}"
