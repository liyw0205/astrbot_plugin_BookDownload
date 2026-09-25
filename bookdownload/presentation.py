from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse, urlsplit, urlunsplit

import aiohttp

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - AstrBot supplies this at runtime
    logger = logging.getLogger(__name__)

from .models import SearchResult
from .network import client_session
from .service import BookSearchService

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
except ImportError:  # pragma: no cover - AstrBot supplies this at runtime
    get_astrbot_temp_path = tempfile.gettempdir

MAX_COVER_BYTES = 5 * 1024 * 1024


@lru_cache(maxsize=16)
def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    bundled_font = Path(__file__).with_name("assets") / "WenQuanYiZenHei.ttc"
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        data_dir = Path(get_astrbot_data_path())
    except ImportError:
        data_dir = Path("/root/AstrBot/data")
    candidates = (
        str(bundled_font),
        str(data_dir / ("font-bold.ttf" if bold else "font.ttf")),
        "/root/AstrBot/data/font-bold.ttf" if bold else "/root/AstrBot/data/font.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/system/fonts/NotoSansCJK-Regular.ttc",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_text(draw, text: str, font, max_width: int, max_lines: int = 3) -> list[str]:
    words = list(str(text or "(untitled)"))
    lines: list[str] = []
    line = ""
    for char in words:
        candidate = line + char
        if line and draw.textlength(candidate, font=font) > max_width:
            lines.append(line)
            line = char
            if len(lines) == max_lines:
                break
        else:
            line = candidate
    if len(lines) < max_lines and line:
        lines.append(line)
    if len(lines) == max_lines and words:
        rendered = "".join(lines)
        if len(rendered) < len(words) and lines[-1]:
            last = lines[-1]
            while last and draw.textlength(last + "…", font=font) > max_width:
                last = last[:-1]
            lines[-1] = last + "…"
    return lines


def _cover_candidates(result: SearchResult) -> list[str]:
    if not result.cover_url or not BookSearchService.is_display_image_url(result.cover_url):
        return []
    candidates = [result.cover_url]
    parsed = urlsplit(result.cover_url)
    host = (parsed.hostname or "").lower()
    if result.source.lower() == "nhentai" and (host == "nhentai.net" or host.endswith(".nhentai.net")):
        for thumbnail_host in ("t.nhentai.net", *(f"t{index}.nhentai.net" for index in range(1, 6))):
            candidate = urlunsplit((parsed.scheme, thumbnail_host, parsed.path, parsed.query, parsed.fragment))
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _result_id(result: SearchResult) -> str:
    """Return the numeric work ID from the canonical result URL."""
    parsed = urlparse(result.url)
    path_parts = [part for part in parsed.path.split("/") if part]
    source = result.source.lower()
    route_names = {"album"} if source in {"jm", "jmcomic"} else {"g"}
    if len(path_parts) >= 2 and path_parts[0].lower() in route_names and path_parts[1].isdigit():
        return path_parts[1]
    query_id = parse_qs(parsed.query).get("id", [""])[0].strip()
    return query_id if query_id.isdigit() else ""


async def _fetch_cover(
    session: aiohttp.ClientSession,
    request_proxy: str | None,
    result: SearchResult,
) -> Any | None:
    candidates = _cover_candidates(result)
    if not candidates:
        return None
    try:
        source = urlparse(result.url)
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AstrBotBookDownload/1.0)",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": f"{source.scheme}://{source.netloc}/",
        }
        from PIL import Image

        for candidate in candidates:
            cover_url = candidate
            for _ in range(6):
                if not BookSearchService.is_display_image_url(cover_url):
                    break
                try:
                    async with session.get(
                        cover_url,
                        proxy=request_proxy,
                        headers=headers,
                        allow_redirects=False,
                    ) as response:
                        if 300 <= response.status < 400:
                            location = response.headers.get("Location")
                            if not location:
                                break
                            cover_url = urljoin(cover_url, location)
                            continue
                        if response.status >= 400:
                            break
                        if response.content_length and response.content_length > MAX_COVER_BYTES:
                            break
                        data = bytearray()
                        too_large = False
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            if len(data) + len(chunk) > MAX_COVER_BYTES:
                                too_large = True
                                break
                            data.extend(chunk)
                    if too_large or not data:
                        break
                    with Image.open(io.BytesIO(data)) as opened:
                        opened.load()
                        return opened.convert("RGB")
                except Exception as exc:
                    logger.debug("Cover request failed for %s: %s", cover_url, exc)
                    break
        logger.debug("No cover loaded for %s after trying %d URLs", result.url, len(candidates))
    except Exception as exc:
        logger.debug("Cover loading failed for %s: %s", result.url, exc)
    return None


async def render_result_card(
    results: list[SearchResult],
    *,
    proxy: str | None,
    timeout: int,
) -> Path:
    from PIL import Image, ImageDraw

    temp_root = Path(get_astrbot_temp_path())
    temp_root.mkdir(parents=True, exist_ok=True)
    descriptor, path = tempfile.mkstemp(prefix="book_results_", suffix=".png", dir=temp_root)
    os.close(descriptor)
    result_path = Path(path)
    request_timeout = aiohttp.ClientTimeout(total=timeout)
    async with client_session(timeout=request_timeout, proxy=proxy) as (session, request_proxy):
        covers = await asyncio.gather(*(_fetch_cover(session, request_proxy, result) for result in results))

    width = 960
    padding = 30
    row_height = 190
    header_height = 110
    height = header_height + padding + max(1, len(results)) * row_height + padding
    canvas = Image.new("RGB", (width, height), "#f3f6f4")
    draw = ImageDraw.Draw(canvas)
    title_font = _font(34, bold=True)
    row_title_font = _font(23, bold=True)
    meta_font = _font(17)
    draw.rounded_rectangle((24, 24, width - 24, 88), radius=12, fill="#ffffff", outline="#dce5e0", width=1)
    draw.text((48, 39), "本子搜索结果", font=title_font, fill="#263b34")
    draw.text((width - 250, 49), f"{len(results)} 个结果", font=meta_font, fill="#477564")

    for index, (result, cover) in enumerate(zip(results, covers), start=1):
        top = header_height + padding + (index - 1) * row_height
        bottom = top + row_height - 12
        draw.rounded_rectangle((padding, top, width - padding, bottom), radius=10, fill="#ffffff", outline="#dce5e0", width=1)
        image_box = (padding + 14, top + 12, padding + 130, bottom - 12)
        if cover is not None:
            cover.thumbnail((image_box[2] - image_box[0], image_box[3] - image_box[1]))
            image_x = image_box[0] + (image_box[2] - image_box[0] - cover.width) // 2
            image_y = image_box[1] + (image_box[3] - image_box[1] - cover.height) // 2
            canvas.paste(cover, (image_x, image_y))
            cover.close()
        else:
            draw.rounded_rectangle(image_box, radius=6, fill="#edf1ef")
            draw.text((image_box[0] + 14, image_box[1] + 55), "无封面", font=meta_font, fill="#64746e")

        text_x = image_box[2] + 24
        max_text_width = width - padding - text_x - 22
        draw.rounded_rectangle((text_x, top + 16, text_x + 112, top + 45), radius=6, fill="#e3f1eb")
        draw.text((text_x + 12, top + 20), result.source.upper(), font=_font(15, bold=True), fill="#315d4d")
        title_y = top + 58
        for line in _wrap_text(draw, result.title, row_title_font, max_text_width, max_lines=2):
            draw.text((text_x, title_y), line, font=row_title_font, fill="#26332e")
            title_y += 31
        meta_parts = []
        result_id = _result_id(result)
        if result_id:
            meta_parts.append(f"ID {result_id}")
        if result.author:
            meta_parts.append(result.author)
        if result.page_count:
            meta_parts.append(f"{result.page_count} 页")
        if result.similarity is not None:
            meta_parts.append(f"相似度 {result.similarity:g}%")
        if meta_parts:
            draw.text((text_x, bottom - 36), "  ·  ".join(meta_parts)[:90], font=meta_font, fill="#65756e")

    canvas.save(result_path, "PNG", optimize=True)
    canvas.close()
    return result_path


def format_text_results(results: list[SearchResult], errors: list[str] | None = None) -> str:
    if not results:
        text = "没有找到匹配结果。"
    else:
        blocks = []
        for index, result in enumerate(results, 1):
            lines = [f"[{index}] {result.title}", f"来源: {result.source}"]
            result_id = _result_id(result)
            if result_id:
                lines.append(f"ID: {result_id}")
            if result.author:
                lines.append(f"作者: {result.author}")
            if result.page_count:
                lines.append(f"页数: {result.page_count}")
            if result.similarity is not None:
                lines.append(f"相似度: {result.similarity:g}%")
            if result.tags:
                lines.append("标签: " + ", ".join(result.tags[:8]))
            lines.append(f"链接: {result.url}")
            blocks.append("\n".join(lines))
        text = "\n\n".join(blocks)
    if errors:
        text += "\n\n部分来源不可用: " + "; ".join(errors)
    return text
