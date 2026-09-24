from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

from .models import GalleryDetail
from .network import client_session, normalize_proxy
from .sources import ehentai_search_url, parse_ehentai_results

NHENTAI_DETAIL = "https://nhentai.net/api/v2/galleries/{gallery_id}"
IMAGE_EXTENSIONS = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}
PAGE_LINK_RE = re.compile(r"^/s/[A-Za-z0-9]+/(\d+)-\d+$")
ALLOWED_DETAIL_HOSTS = {"nhentai.net", "e-hentai.org", "exhentai.org"}
ALLOWED_IMAGE_HOSTS = {
    "nhentai.net",
    "e-hentai.org",
    "exhentai.org",
    "ehgt.org",
    "hath.network",
}


class DetailError(RuntimeError):
    """A user-facing detail lookup failure."""


def parse_gallery_reference(value: str, source_hint: str = "") -> tuple[str, str, str]:
    raw = str(value or "").strip()
    hint = str(source_hint or "").strip().lower()
    if raw.isdigit():
        if hint not in {"nhentai", "ehentai"}:
            raise DetailError("输入作品 ID 时，必须通过 nh查看 或 eh查看 指定来源。")
        return hint, raw, ""
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or host not in ALLOWED_DETAIL_HOSTS:
        raise DetailError("只支持 HTTPS 的 NHentai 或 E-Hentai 作品链接。")
    match = re.match(r"^/g/(\d+)(?:/|$)", parsed.path)
    if not match:
        raise DetailError("链接必须是作品页，例如 https://nhentai.net/g/123456/。")
    source = "nhentai" if host == "nhentai.net" else "ehentai"
    return source, match.group(1), raw


def _tag_values(payload: Any, category: str = "") -> tuple[str, ...]:
    values: list[str] = []
    if not isinstance(payload, list):
        return ()
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        item_type = str(item.get("type", category)).strip().lower()
        values.append(f"{item_type}:{name}" if item_type and item_type not in {"tag", ""} else name)
    return tuple(dict.fromkeys(values))


class GalleryDetailService:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.proxy = normalize_proxy(config.get("proxy_url", ""))
        self.timeout = self._bounded_int(config.get("timeout"), 30, 5, 120)
        self.cookie = str(config.get("ehentai_cookie", "")).strip()

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    async def fetch(self, value: str, source_hint: str = "") -> GalleryDetail:
        source, gallery_id, gallery_url = parse_gallery_reference(value, source_hint)
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        headers = {"User-Agent": "astrbot_plugin_BookDownload/0.6.0", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"}
        async with client_session(timeout=timeout, headers=headers, proxy=self.proxy) as (session, request_proxy):
            if source == "nhentai":
                return await self._nhentai(session, request_proxy, gallery_id)
            return await self._ehentai(session, request_proxy, gallery_id, gallery_url)

    async def _nhentai(self, session: aiohttp.ClientSession, request_proxy: str | None, gallery_id: str) -> GalleryDetail:
        url = NHENTAI_DETAIL.format(gallery_id=gallery_id)
        async with session.get(url, proxy=request_proxy, headers={"Referer": "https://nhentai.net/"}) as response:
            if response.status >= 400:
                raise DetailError(f"NHentai 详情请求失败（HTTP {response.status}）。")
            try:
                payload = await response.json(content_type=None)
            except Exception as exc:
                raise DetailError("NHentai 返回的详情不是有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise DetailError("NHentai 详情格式无效。")
        title_data = payload.get("title") or {}
        if isinstance(title_data, dict):
            title = next((str(title_data.get(key, "")).strip() for key in ("pretty", "english", "japanese") if title_data.get(key)), "本子")
        else:
            title = str(title_data or "本子").strip()
        tags_data = payload.get("tags") or []
        tags = _tag_values(tags_data)
        by_type: dict[str, list[str]] = {"language": [], "artist": [], "group": []}
        for item in tags_data if isinstance(tags_data, list) else []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type", "")).strip().lower()
            name = str(item.get("name", "")).strip()
            if name and kind in by_type:
                by_type[kind].append(name)
        thumb = payload.get("thumbnail") or {}
        cover_path = str(thumb.get("path", "")).strip().lstrip("/") if isinstance(thumb, dict) else ""
        if not cover_path:
            cover = payload.get("cover") or {}
            cover_path = str(cover.get("path", "")).strip().lstrip("/") if isinstance(cover, dict) else ""
        cover_url = f"https://t1.nhentai.net/{cover_path}" if cover_path.startswith("galleries/") else ""
        media_id = str(payload.get("media_id", "")).strip()
        page_urls: list[str] = []
        pages = payload.get("pages") or []
        if isinstance(pages, list):
            for index, page in enumerate(pages, start=1):
                page_path = str(page.get("path", "")).strip().lstrip("/") if isinstance(page, dict) else ""
                if page_path.startswith("galleries/"):
                    page_urls.append(f"https://i.nhentai.net/{page_path}")
                    continue
                ext = IMAGE_EXTENSIONS.get(str(page.get("t", "j")).lower(), "jpg") if isinstance(page, dict) else "jpg"
                if media_id.isdigit():
                    page_urls.append(f"https://i.nhentai.net/galleries/{media_id}/{index}.{ext}")
        page_count = payload.get("num_pages")
        if not isinstance(page_count, int):
            page_count = len(page_urls) or None
        return GalleryDetail(
            source="nhentai", gallery_id=gallery_id, title=title,
            url=f"https://nhentai.net/g/{gallery_id}/", cover_url=cover_url,
            tags=tags, languages=tuple(dict.fromkeys(by_type["language"])),
            artists=tuple(dict.fromkeys(by_type["artist"])), groups=tuple(dict.fromkeys(by_type["group"])),
            page_count=page_count, page_urls=tuple(page_urls),
        )

    def _eh_headers(self, referer: str) -> dict[str, str]:
        headers = {"Referer": referer, "User-Agent": "Mozilla/5.0", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    async def _get_text(self, session: aiohttp.ClientSession, request_proxy: str | None, url: str, headers: dict[str, str]) -> str:
        async with session.get(url, proxy=request_proxy, headers=headers) as response:
            if response.status >= 400:
                raise DetailError(f"请求站点失败（HTTP {response.status}）。")
            return await response.text()

    async def _ehentai(self, session: aiohttp.ClientSession, request_proxy: str | None, gallery_id: str, gallery_url: str) -> GalleryDetail:
        site = str(self.config.get("ehentai_site", "e-hentai")).strip().lower()
        if site not in {"e-hentai", "exhentai"}:
            raise DetailError("ehentai_site 只能是 e-hentai 或 exhentai。")
        base_url = f"https://{site}.org/"
        if not gallery_url:
            html = await self._get_text(session, request_proxy, ehentai_search_url(f"gid:{gallery_id}", 1, base_url), self._eh_headers(base_url))
            matches = [item for item in parse_ehentai_results(html, base_url) if urlparse(item.url).path.split("/")[2:3] == [gallery_id]]
            if not matches:
                raise DetailError(f"E-Hentai 未找到作品 ID {gallery_id}，请改用完整作品链接。")
            gallery_url = matches[0].url
        html = await self._get_text(session, request_proxy, gallery_url, self._eh_headers(gallery_url))
        soup = BeautifulSoup(html, "html.parser")
        title_node = soup.select_one("#gn, #gj")
        title = title_node.get_text(" ", strip=True) if title_node else "本子"
        tags: list[str] = []
        languages: list[str] = []
        artists: list[str] = []
        groups: list[str] = []
        for row in soup.select("#taglist tr"):
            cells = row.select("td")
            if len(cells) < 2:
                continue
            category = cells[0].get_text(" ", strip=True).rstrip(":").strip().lower()
            names = [node.get_text(" ", strip=True) for node in cells[1].select("a") if node.get_text(" ", strip=True)]
            for name in names:
                tags.append(f"{category}:{name}" if category else name)
                if category == "language": languages.append(name)
                elif category == "artist": artists.append(name)
                elif category == "group": groups.append(name)
        if not languages:
            for row in soup.select("#gdd tr"):
                text = row.get_text(" ", strip=True)
                if text.lower().startswith("language:"):
                    value = re.sub(r"^language:\s*", "", text, flags=re.I)
                    languages.extend(re.findall(r"[A-Za-z][A-Za-z -]+", value))
        cover_url = ""
        cover_node = soup.select_one("#gleft [style*='background']")
        if cover_node:
            match = re.search(r"url\(['\"]?([^)'\"]+)", str(cover_node.get("style", "")), flags=re.I)
            if match:
                cover_url = urljoin(gallery_url, match.group(1))
        page_links: list[str] = []
        for anchor in soup.select("#gdt a[href]"):
            absolute = urljoin(gallery_url, str(anchor.get("href", "")))
            parsed = urlparse(absolute)
            if parsed.netloc == urlparse(gallery_url).netloc and PAGE_LINK_RE.match(parsed.path) and absolute not in page_links:
                page_links.append(absolute)
        page_urls: list[str] = []
        for page_url in page_links[:4]:
            page_html = await self._get_text(session, request_proxy, page_url, self._eh_headers(gallery_url))
            page_soup = BeautifulSoup(page_html, "html.parser")
            image = page_soup.select_one("#img, img#img")
            image_url = str(image.get("src", "")).strip() if image else ""
            if image_url:
                page_urls.append(urljoin(page_url, image_url))
        page_count = None
        for row in soup.select("#gdd tr"):
            match = re.search(r"(?:Length|pages?)\s*:\s*(\d+)\s*pages?", row.get_text(" ", strip=True), flags=re.I)
            if match:
                page_count = int(match.group(1)); break
        page_count = page_count or len(page_links) or None
        return GalleryDetail(
            source="ehentai", gallery_id=gallery_id, title=title, url=gallery_url,
            cover_url=cover_url, tags=tuple(dict.fromkeys(tags)),
            languages=tuple(dict.fromkeys(languages)), artists=tuple(dict.fromkeys(artists)),
            groups=tuple(dict.fromkeys(groups)), page_count=page_count, page_urls=tuple(page_urls),
        )
