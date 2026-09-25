from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .models import GalleryDetail, SearchResult

JM_SITE_DOMAINS = (
    "18comic.vip",
    "18comic.org",
    "18comic.me",
    "18comic.com",
    "jmcomic.me",
    "jmcomic.org",
    "jmcomic.cc",
    "jmcomic.com",
)
JM_IMAGE_DOMAINS = (
    "jmapiproxy1.cc",
    "jmapiproxy2.cc",
    "jmapinodeudzn.net",
)
JM_CANONICAL_URL = "https://18comic.vip/album/{album_id}/"


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    host = str(host or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def jm_album_id_from_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme.lower() != "https" or not _host_matches(parsed.hostname or "", JM_SITE_DOMAINS):
        return ""
    match = re.match(r"^/(?:album|albums)/(\d+)(?:/|$)", parsed.path, re.IGNORECASE)
    if match:
        return match.group(1)
    if re.match(r"^/albums?/?$", parsed.path, re.IGNORECASE):
        album_id = parse_qs(parsed.query).get("id", [""])[0]
        return album_id if album_id.isdigit() else ""
    return ""


def _load_jmcomic():
    try:
        from jmcomic import JmOption, JmcomicText, download_album_async
    except ImportError as exc:
        raise RuntimeError("JM 来源需要安装 jmcomic 依赖；请在 AstrBot 插件管理页更新依赖后重载插件。") from exc
    return JmOption, JmcomicText, download_album_async


def create_jm_option(*, proxy: str | None, timeout: int, base_dir: str | Path | None = None):
    JmOption, _, _ = _load_jmcomic()
    config = {
        "log": False,
        "client": {
            "timeout": timeout,
            "retry_times": 3,
            "postman": {"meta_data": {"proxies": proxy or {}}},
        },
        "download": {"threading": {"image": 4, "photo": 2}},
    }
    if base_dir is not None:
        config["dir_rule"] = {"rule": "Bd_Pname", "base_dir": str(base_dir)}
    return JmOption.construct(config)


def _text_tuple(value) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        values = re.split(r"[,，、\s]+", value)
    else:
        values = [str(item) for item in value]
    return tuple(dict.fromkeys(item.strip() for item in values if item and item.strip()))


def _jm_languages(tags: tuple[str, ...]) -> tuple[str, ...]:
    values = [tag for tag in tags if re.search(r"chinese|中文|漢化|汉化|繁體|简体|繁体", tag, re.IGNORECASE)]
    if any(re.search(r"中文|漢化|汉化|繁體|简体|繁体", tag, re.IGNORECASE) for tag in values):
        values.append("chinese")
    return tuple(dict.fromkeys(values))


async def search_jmcomic(query: str, page: int, *, proxy: str | None, timeout: int) -> list[SearchResult]:
    _, JmcomicText, _ = _load_jmcomic()
    option = create_jm_option(proxy=proxy, timeout=timeout)
    async with option.new_jm_async_client(max_clients=4) as client:
        search_page = await client.search_site(query, page=page)
    results: list[SearchResult] = []
    for album_id, title, raw_tags in search_page.iter_id_title_tag():
        album_id = str(album_id).strip()
        if not album_id.isdigit():
            continue
        tags = _text_tuple(raw_tags)
        results.append(
            SearchResult(
                source="jmcomic",
                title=str(title or "(untitled)").strip(),
                url=JM_CANONICAL_URL.format(album_id=album_id),
                cover_url=JmcomicText.get_album_cover_url(album_id, size="_3x4"),
                tags=tags,
            )
        )
    return results


class _NamedBytesIO(io.BytesIO):
    def __init__(self, name: str):
        super().__init__()
        self.name = name


def _decode_preview(data: bytes, image_detail) -> bytes:
    from jmcomic import JmImageTool

    suffix = str(image_detail.img_file_suffix or ".jpg").lower()
    if suffix == ".gif":
        return data
    source = JmImageTool.open_image(data)
    output = _NamedBytesIO("preview" + suffix)
    try:
        scramble = JmImageTool.get_num(
            int(image_detail.scramble_id),
            int(image_detail.aid),
            image_detail.img_file_name,
        )
        JmImageTool.decode_and_save(scramble, source, output)
        return output.getvalue()
    finally:
        source.close()
        output.close()


async def fetch_jm_detail(
    album_id: str,
    *,
    proxy: str | None,
    timeout: int,
    include_previews: bool,
) -> GalleryDetail:
    _, JmcomicText, _ = _load_jmcomic()
    option = create_jm_option(proxy=proxy, timeout=timeout)
    async with option.new_jm_async_client(max_clients=4) as client:
        album = await client.get_album_detail(album_id)
        tags = _text_tuple(getattr(album, "tags", ()))
        page_images: list[bytes] = []
        if include_previews and getattr(album, "episode_list", None):
            photo_id = str(album.episode_list[0][0])
            photo = await client.get_photo_detail(photo_id)
            for index in range(min(6, len(photo))):
                image_detail = photo.create_image_detail(index)
                response = await client.get_jm_image(image_detail.download_url)
                page_images.append(await asyncio.to_thread(_decode_preview, response.content, image_detail))

    authors = _text_tuple(getattr(album, "authors", ()))
    works = _text_tuple(getattr(album, "works", ()))
    return GalleryDetail(
        source="jmcomic",
        gallery_id=str(album.album_id),
        title=str(album.name or "本子"),
        url=JM_CANONICAL_URL.format(album_id=album.album_id),
        cover_url=JmcomicText.get_album_cover_url(album.album_id, size="_3x4"),
        tags=tags,
        languages=_jm_languages(tags),
        artists=authors,
        groups=works,
        page_count=int(album.page_count) if album.page_count is not None else None,
        preview_images=tuple(page_images),
    )


async def download_jm_album(
    album_id: str,
    root: Path,
    *,
    proxy: str | None,
    timeout: int,
    max_pages: int,
    max_mb: int,
) -> tuple[str, list[Path]]:
    _, _, download_album_async = _load_jmcomic()
    option = create_jm_option(proxy=proxy, timeout=timeout, base_dir=root)
    async with option.new_jm_async_client(max_clients=4) as client:
        album = await client.get_album_detail(album_id)
        page_count = int(album.page_count)
    if page_count > max_pages:
        raise ValueError(f"作品共 {page_count} 页，超过当前下载上限 {max_pages} 页。")

    result = await download_album_async(album_id, option, check_exception=True)
    image_paths = [Path(path) for path in result.manifest.image_filepath_list]
    if not image_paths:
        raise ValueError("JM 下载未生成页面图片。")
    root_resolved = root.resolve()
    if any(not path.resolve().is_relative_to(root_resolved) for path in image_paths):
        raise ValueError("JM 下载产物超出插件临时目录，已中止后续处理。")
    total_bytes = sum(path.stat().st_size for path in image_paths)
    if total_bytes > max_mb * 1024 * 1024:
        raise ValueError(f"下载内容超过 {max_mb} MB 限制。")
    return str(result.detail.name or album.name or "本子"), image_paths
