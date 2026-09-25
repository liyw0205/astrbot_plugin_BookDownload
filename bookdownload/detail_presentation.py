from __future__ import annotations

import asyncio
import io
import os
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp

from .details import ALLOWED_IMAGE_HOSTS
from .models import GalleryDetail
from .network import client_session
from .presentation import _font, _wrap_text

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
except ImportError:  # pragma: no cover - AstrBot supplies this at runtime
    get_astrbot_temp_path = tempfile.gettempdir

MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _allowed_image_url(value: str) -> bool:
    parsed = urlparse(str(value or ""))
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(host == domain or host.endswith("." + domain) for domain in ALLOWED_IMAGE_HOSTS)


async def _fetch_image(session: aiohttp.ClientSession, request_proxy: str | None, value: str, referer: str) -> object | None:
    from PIL import Image

    current = str(value or "").strip()
    for _ in range(5):
        if not _allowed_image_url(current):
            return None
        try:
            async with session.get(
                current,
                proxy=request_proxy,
                headers={"User-Agent": "Mozilla/5.0", "Referer": referer, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"},
                allow_redirects=False,
            ) as response:
                if 300 <= response.status < 400:
                    location = response.headers.get("Location")
                    if not location:
                        return None
                    current = urljoin(current, location)
                    continue
                if response.status >= 400:
                    return None
                if response.content_length and response.content_length > MAX_IMAGE_BYTES:
                    return None
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                        return None
                    data.extend(chunk)
            with Image.open(io.BytesIO(data)) as opened:
                opened.load()
                return opened.convert("RGB")
        except Exception:
            return None
    return None


def _draw_labeled_text(draw, x: int, y: int, label: str, value: str, font, value_font, max_width: int, max_lines: int = 3) -> int:
    if not value:
        return y
    draw.text((x, y), f"{label}:", font=font, fill="#4f6c61")
    label_width = int(draw.textlength(f"{label}:", font=font)) + 10
    lines = _wrap_text(draw, value, value_font, max_width - label_width, max_lines=max_lines)
    draw.text((x + label_width, y), lines[0], font=value_font, fill="#24352e")
    y += 27
    for line in lines[1:]:
        draw.text((x + label_width, y), line, font=value_font, fill="#24352e")
        y += 24
    return y + 6


async def render_detail_card(detail: GalleryDetail, *, proxy: str | None, timeout: int) -> Path:
    from PIL import Image, ImageDraw, ImageOps

    temp_root = Path(get_astrbot_temp_path())
    temp_root.mkdir(parents=True, exist_ok=True)
    descriptor, path = tempfile.mkstemp(prefix="book_detail_", suffix=".png", dir=temp_root)
    os.close(descriptor)
    output = Path(path)
    request_timeout = aiohttp.ClientTimeout(total=timeout)
    async with client_session(timeout=request_timeout, proxy=proxy) as (session, request_proxy):
        referer = detail.url
        cover, *remote_pages = await asyncio.gather(
            _fetch_image(session, request_proxy, detail.cover_url, referer),
            *(
                _fetch_image(session, request_proxy, page_url, referer)
                for page_url in detail.page_urls[: max(0, 6 - len(detail.preview_images))]
            ),
        )
    preview_pages = []
    for data in detail.preview_images[:6]:
        try:
            with Image.open(io.BytesIO(data)) as opened:
                opened.load()
                preview_pages.append(opened.convert("RGB"))
        except Exception:
            preview_pages.append(None)
    pages = preview_pages + remote_pages

    width, top_height = 820, 450
    padding, gap = 24, 12
    preview_width, preview_height = 300, 410
    section_height = 38 + preview_height * 3 + gap * 2
    height = top_height + section_height + padding * 2 + 18
    canvas = Image.new("RGB", (width, height), "#f4f7f5")
    draw = ImageDraw.Draw(canvas)
    title_font = _font(34, bold=True)
    section_font = _font(22, bold=True)
    label_font = _font(17, bold=True)
    body_font = _font(17)
    small_font = _font(15)

    draw.rounded_rectangle((padding, padding, width - padding, top_height), radius=14, fill="#ffffff", outline="#d8e3dd", width=2)
    cover_box = (padding + 14, padding + 14, padding + 274, top_height - 14)
    draw.rounded_rectangle(cover_box, radius=8, fill="#edf2ef")
    if cover is not None:
        cover_img = ImageOps.fit(cover, (cover_box[2] - cover_box[0] - 8, cover_box[3] - cover_box[1] - 8), method=Image.Resampling.LANCZOS)
        canvas.paste(cover_img, (cover_box[0] + (cover_box[2] - cover_box[0] - cover_img.width) // 2, cover_box[1] + (cover_box[3] - cover_box[1] - cover_img.height) // 2))
        cover.close()
    else:
        draw.text((cover_box[0] + 86, cover_box[1] + 205), "无封面", font=body_font, fill="#71827a")

    info_x = cover_box[2] + 28
    info_width = width - padding - info_x - 22
    title_lines = _wrap_text(draw, detail.title, title_font, info_width, max_lines=3)
    y = padding + 24
    for line in title_lines:
        draw.text((info_x, y), line, font=title_font, fill="#1f332b")
        y += 42
    draw.text((info_x, y + 4), f"{detail.source.upper()}  ·  ID {detail.gallery_id}", font=small_font, fill="#527367")
    y += 38
    y = _draw_labeled_text(draw, info_x, y, "Tags", ", ".join(detail.tags), label_font, body_font, info_width, 3)
    y = _draw_labeled_text(draw, info_x, y, "Languages", ", ".join(detail.languages), label_font, body_font, info_width, 2)
    y = _draw_labeled_text(draw, info_x, y, "Pages", str(detail.page_count or len(detail.page_urls) or "-"), label_font, body_font, info_width, 1)
    y = _draw_labeled_text(draw, info_x, y, "Artists", ", ".join(detail.artists), label_font, body_font, info_width, 2)
    _draw_labeled_text(draw, info_x, y, "Groups", ", ".join(detail.groups), label_font, body_font, info_width, 2)

    section_y = top_height + padding
    draw.text((padding, section_y), "前 6 页预览", font=section_font, fill="#29443a")
    grid_top = section_y + 38
    grid_left = (width - preview_width * 2 - gap) // 2
    for index in range(6):
        row, col = divmod(index, 2)
        left = grid_left + col * (preview_width + gap)
        top = grid_top + row * (preview_height + gap)
        box = (left, top, left + preview_width, top + preview_height)
        draw.rounded_rectangle(box, radius=10, fill="#ffffff", outline="#d8e3dd", width=1)
        page = pages[index] if index < len(pages) else None
        if page is not None:
            page_img = ImageOps.contain(page, (preview_width - 12, preview_height - 32))
            canvas.paste(page_img, (left + (preview_width - page_img.width) // 2, top + 24 + (preview_height - 24 - page_img.height) // 2))
            page.close()
        else:
            draw.text((left + preview_width // 2 - 32, top + 190), "无预览", font=body_font, fill="#71827a")
        draw.text((left + 12, top + 6), f"第 {index + 1} 页", font=small_font, fill="#527367")

    canvas.save(output, "PNG", optimize=True)
    canvas.close()
    return output
