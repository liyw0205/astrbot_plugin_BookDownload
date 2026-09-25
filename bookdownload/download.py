from __future__ import annotations

import asyncio
import io
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

from .network import client_session, normalize_proxy
from .jmcomic_source import download_jm_album, jm_album_id_from_url
from .sources import ehentai_search_url, parse_ehentai_results

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
except ImportError:  # pragma: no cover - AstrBot supplies this at runtime
    get_astrbot_temp_path = tempfile.gettempdir


NHENTAI_DETAIL = "https://nhentai.net/api/v2/galleries/{gallery_id}"
ALLOWED_HOSTS = {"nhentai.net", "e-hentai.org", "exhentai.org"}
IMAGE_EXTENSIONS = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}
PAGE_LINK_RE = re.compile(r"^/s/[A-Za-z0-9]+/(\d+)-\d+$")


class DownloadError(RuntimeError):
    """A user-facing download failure."""


@dataclass
class DownloadResult:
    title: str
    format: str
    root: Path
    image_paths: list[Path]
    output_paths: list[Path]

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def normalize_download_format(value: Any, default: str = "archive") -> str:
    aliases = {
        "pdf": "pdf",
        "压缩包": "archive",
        "压缩": "archive",
        "zip": "archive",
        "cbz": "archive",
        "archive": "archive",
        "图片": "images",
        "图片原图": "images",
        "image": "images",
        "images": "images",
        "长图": "long_image",
        "long": "long_image",
        "long_image": "long_image",
        "longimage": "long_image",
    }
    raw = str(value or "").strip().lower()
    if not raw:
        raw = str(default or "archive").strip().lower()
    try:
        return aliases[raw]
    except KeyError as exc:
        raise ValueError("发送方式只能是 pdf、压缩包、图片或长图。") from exc


def _safe_title(value: str) -> str:
    title = re.sub(r"[\\/:*?\"<>|\x00\r\n]+", "_", str(value or "本子")).strip(" ._")
    return (title or "本子")[:100]


def _image_extension(data: bytes, fallback: str = "jpg") -> str:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            fmt = str(image.format or "").lower()
        return {"jpeg": "jpg", "jpg": "jpg", "png": "png", "gif": "gif", "webp": "webp"}.get(fmt, fallback)
    except Exception:
        return fallback


class BookDownloadService:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.proxy = normalize_proxy(config.get("proxy_url", ""))
        self.timeout = self._bounded_int(config.get("timeout"), 30, 5, 120)
        self.max_pages = self._bounded_int(config.get("download_max_pages"), 100, 1, 300)
        self.max_mb = self._bounded_int(config.get("download_max_mb"), 200, 10, 1000)
        self.cookie = str(config.get("ehentai_cookie", "")).strip()

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str]:
        jm_id = jm_album_id_from_url(url)
        if jm_id:
            return "jmcomic", jm_id
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme.lower() != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise DownloadError("只支持 HTTPS 的 NHentai、E-Hentai 或 JM 作品链接。")
        match = re.match(r"^/g/(\d+)(?:/|$)", parsed.path)
        if not match:
            raise DownloadError("链接必须是作品页，例如 https://nhentai.net/g/123456/ 或 https://18comic.vip/album/123456/。")
        source = "nhentai" if parsed.hostname == "nhentai.net" else "ehentai"
        return source, match.group(1)

    def _headers(self, referer: str) -> dict[str, str]:
        headers = {
            "User-Agent": "astrbot_plugin_BookDownload/0.5.0",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Referer": referer,
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    async def download(self, url: str, output_format: str = "", source_hint: str = "") -> DownloadResult:
        selected_format = normalize_download_format(output_format, self.config.get("download_format", "archive"))
        raw_url = str(url or "").strip()
        source = str(source_hint or "").strip().lower()
        gallery_id = ""
        if raw_url.isdigit():
            if source not in {"nhentai", "ehentai", "jmcomic"}:
                raise DownloadError("输入作品 ID 时，必须通过 nh下载、eh下载 或 jm下载 指定来源。")
            gallery_id = raw_url
            gallery_url = f"https://nhentai.net/g/{gallery_id}/" if source == "nhentai" else ""
        elif re.fullmatch(r"(?i:jm)\d+", raw_url) and source == "jmcomic":
            gallery_id = re.sub(r"(?i)^jm", "", raw_url)
            gallery_url = ""
        else:
            source, gallery_id = self._parse_url(raw_url)
            gallery_url = raw_url
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        temp_parent = Path(get_astrbot_temp_path())
        temp_parent.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="bookdownload_", dir=str(temp_parent)))
        try:
            if source == "jmcomic":
                title, image_paths = await download_jm_album(
                    gallery_id,
                    root,
                    proxy=self.proxy,
                    timeout=self.timeout,
                    max_pages=self.max_pages,
                    max_mb=self.max_mb,
                )
            else:
                async with client_session(timeout=timeout, headers=self._headers(str(url)), proxy=self.proxy) as (session, request_proxy):
                    if source == "nhentai":
                        title, page_urls = await self._nhentai_pages(session, request_proxy, gallery_id)
                    else:
                        if not gallery_url:
                            gallery_url = await self._resolve_ehentai_id(session, request_proxy, gallery_id)
                        title, page_urls = await self._ehentai_pages(session, request_proxy, gallery_url, gallery_id)
                    if not page_urls:
                        raise DownloadError("没有找到可下载的页面，可能是链接失效或站点限制访问。")
                    if len(page_urls) > self.max_pages:
                        page_urls = page_urls[: self.max_pages]
                    image_paths = await self._download_pages(session, request_proxy, page_urls, root)
            if not image_paths:
                raise DownloadError("页面图片下载失败。")
            output_paths = self._build_outputs(selected_format, _safe_title(title), root, image_paths)
            return DownloadResult(_safe_title(title), selected_format, root, image_paths, output_paths)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

    async def _resolve_ehentai_id(
        self,
        session: aiohttp.ClientSession,
        request_proxy: str | None,
        gallery_id: str,
    ) -> str:
        site = str(self.config.get("ehentai_site", "e-hentai")).strip().lower()
        if site not in {"e-hentai", "exhentai"}:
            raise DownloadError("ehentai_site 只能是 e-hentai 或 exhentai。")
        base_url = f"https://{site}.org/"
        search_url = ehentai_search_url(f"gid:{gallery_id}", 1, base_url)
        html = await self._get_text(session, request_proxy, search_url, self._headers(base_url))
        for result in parse_ehentai_results(html, base_url):
            if urlparse(result.url).path.split("/")[2:3] == [gallery_id]:
                return result.url
        raise DownloadError(f"E-Hentai 未找到作品 ID {gallery_id}，请改用完整作品链接。")

    async def _get_text(self, session: aiohttp.ClientSession, request_proxy: str | None, url: str, headers: dict[str, str]) -> str:
        async with session.get(url, proxy=request_proxy, headers=headers) as response:
            if response.status >= 400:
                raise DownloadError(f"请求站点失败（HTTP {response.status}）。")
            return await response.text()

    async def _nhentai_pages(self, session: aiohttp.ClientSession, request_proxy: str | None, gallery_id: str) -> tuple[str, list[str]]:
        url = NHENTAI_DETAIL.format(gallery_id=gallery_id)
        async with session.get(url, proxy=request_proxy, headers=self._headers("https://nhentai.net/")) as response:
            if response.status >= 400:
                raise DownloadError(f"NHentai 详情请求失败（HTTP {response.status}）。")
            try:
                payload = await response.json(content_type=None)
            except Exception as exc:
                raise DownloadError("NHentai 返回的详情不是有效 JSON。") from exc
        media_id = str(payload.get("media_id", "")).strip()
        pages = payload.get("pages") or []
        if not media_id.isdigit() or not isinstance(pages, list):
            raise DownloadError("NHentai 详情缺少页面信息。")
        title_data = payload.get("title") or {}
        if isinstance(title_data, dict):
            title = next((str(title_data.get(key, "")).strip() for key in ("english", "pretty", "japanese") if title_data.get(key)), "本子")
        else:
            title = str(title_data or "本子")
        page_urls: list[str] = []
        for index, page in enumerate(pages, start=1):
            page = page if isinstance(page, dict) else {}
            page_path = str(page.get("path", "")).strip().lstrip("/")
            if page_path.startswith("galleries/"):
                page_urls.append(f"https://i.nhentai.net/{page_path}")
                continue
            ext = IMAGE_EXTENSIONS.get(str(page.get("t", "j")).lower(), "jpg")
            page_urls.append(f"https://i.nhentai.net/galleries/{media_id}/{index}.{ext}")
        return title, page_urls

    async def _ehentai_pages(self, session: aiohttp.ClientSession, request_proxy: str | None, gallery_url: str, gallery_id: str) -> tuple[str, list[str]]:
        parsed = urlparse(gallery_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        headers = self._headers(gallery_url)
        page_links: list[str] = []
        seen_gallery_pages: set[str] = set()
        next_url = gallery_url
        for _ in range(50):
            if next_url in seen_gallery_pages:
                break
            seen_gallery_pages.add(next_url)
            html = await self._get_text(session, request_proxy, next_url, headers)
            soup = BeautifulSoup(html, "html.parser")
            for anchor in soup.select("#gdt a[href], a[href]"):
                href = str(anchor.get("href", ""))
                absolute = urljoin(base, href)
                parsed_page = urlparse(absolute)
                if parsed_page.netloc != parsed.netloc or not PAGE_LINK_RE.match(parsed_page.path):
                    continue
                if absolute not in page_links:
                    page_links.append(absolute)
            if len(page_links) >= self.max_pages:
                break
            candidates: list[tuple[int, str]] = []
            current_page = int(parse_qs(parsed_page_query(next_url)).get("p", ["0"])[0] or 0)
            for anchor in soup.select("a[href]"):
                href = urljoin(base, str(anchor.get("href", "")))
                candidate_page = parse_qs(urlparse(href).query).get("p", [""])[0]
                if candidate_page.isdigit() and int(candidate_page) > current_page and urlparse(href).path == parsed.path:
                    candidates.append((int(candidate_page), href))
            if not candidates:
                break
            next_url = min(candidates)[1]
        title = "本子"
        if "soup" in locals():
            title_node = soup.select_one("#gn, #gj")
            if title_node:
                title = title_node.get_text(" ", strip=True) or title
        page_urls: list[str] = []
        for page_url in page_links[: self.max_pages]:
            page_html = await self._get_text(session, request_proxy, page_url, headers)
            page_soup = BeautifulSoup(page_html, "html.parser")
            image = page_soup.select_one("#img, img#img")
            image_url = str(image.get("src", "")) if image else ""
            if image_url:
                page_urls.append(urljoin(page_url, image_url))
        if not page_urls and page_links:
            raise DownloadError(f"E-Hentai 作品 {gallery_id} 的原图页面无法解析。")
        return title, page_urls

    async def _download_pages(self, session: aiohttp.ClientSession, request_proxy: str | None, urls: list[str], root: Path) -> list[Path]:
        semaphore = asyncio.Semaphore(4)
        total_bytes = 0
        total_limit = self.max_mb * 1024 * 1024
        total_lock = asyncio.Lock()

        async def download_one(index: int, url: str) -> Path:
            nonlocal total_bytes
            async with semaphore:
                data = b""
                last_status = 0
                candidates = [url]
                parsed = urlparse(url)
                if parsed.hostname == "i.nhentai.net":
                    candidates.extend(url.replace("https://i.nhentai.net/", f"https://i{suffix}.nhentai.net/", 1) for suffix in ("1", "2", "3", "4", "5"))
                for candidate in candidates:
                    try:
                        async with session.get(candidate, proxy=request_proxy, headers=self._headers(candidate)) as response:
                            last_status = response.status
                            if response.status < 400:
                                data = await response.read()
                                break
                    except aiohttp.ClientError:
                        continue
                if not data:
                    raise DownloadError(f"第 {index} 页下载失败（HTTP {last_status or '网络错误'}）。")
                async with total_lock:
                    total_bytes += len(data)
                    if total_bytes > total_limit:
                        raise DownloadError(f"下载内容超过 {self.max_mb} MB 限制。")
                extension = _image_extension(data)
                path = root / f"{index:04d}.{extension}"
                path.write_bytes(data)
                return path

        results = await asyncio.gather(*(download_one(index, url) for index, url in enumerate(urls, start=1)), return_exceptions=True)
        errors = [item for item in results if isinstance(item, Exception)]
        if errors:
            raise errors[0]
        return [item for item in results if isinstance(item, Path)]

    def _build_outputs(self, selected_format: str, title: str, root: Path, image_paths: list[Path]) -> list[Path]:
        if selected_format == "images":
            return image_paths
        if selected_format == "archive":
            archive_path = root / f"{title}.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in image_paths:
                    archive.write(path, arcname=path.relative_to(root).as_posix())
            return [archive_path]
        try:
            from PIL import Image
        except ImportError as exc:
            raise DownloadError("PDF/长图需要安装 Pillow。") from exc
        if selected_format == "pdf":
            pdf_path = root / f"{title}.pdf"
            images = [Image.open(path).convert("RGB") for path in image_paths]
            try:
                first, rest = images[0], images[1:]
                first.save(pdf_path, "PDF", save_all=True, append_images=rest, resolution=150.0)
            finally:
                for image in images:
                    image.close()
            return [pdf_path]
        outputs: list[Path] = []
        for offset in range(0, len(image_paths), 10):
            chunk = image_paths[offset : offset + 10]
            images = [Image.open(path).convert("RGB") for path in chunk]
            try:
                max_width = max(image.width for image in images)
                canvas = Image.new("RGB", (max_width, sum(image.height for image in images)), "white")
                y = 0
                for image in images:
                    canvas.paste(image, ((max_width - image.width) // 2, y))
                    y += image.height
                output = root / f"{title}_{offset // 10 + 1:03d}.jpg"
                canvas.save(output, "JPEG", quality=88, optimize=True)
                canvas.close()
                outputs.append(output)
            finally:
                for image in images:
                    image.close()
        return outputs


def parsed_page_query(url: str) -> str:
    return urlparse(url).query
