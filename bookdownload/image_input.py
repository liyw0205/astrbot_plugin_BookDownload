from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import socket
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp

from .network import client_session, normalize_proxy


class ImageInputError(ValueError):
    pass


def _is_image(data: bytes) -> bool:
    return data.startswith((
        b"\xff\xd8\xff",
        b"\x89PNG\r\n\x1a\n",
        b"GIF87a",
        b"GIF89a",
        b"BM",
    )) or (len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")


async def _public_https_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return False
        port = parsed.port or 443
        try:
            addresses = [ipaddress.ip_address(parsed.hostname)]
        except ValueError:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                parsed.hostname,
                port,
                type=socket.SOCK_STREAM,
            )
            addresses = [ipaddress.ip_address(record[4][0]) for record in records]
        return bool(addresses) and all(address.is_global for address in addresses)
    except (OSError, ValueError):
        return False


async def _download_public_image(
    url: str,
    max_bytes: int,
    timeout: int,
    proxy: str | None,
) -> bytes:
    current = url
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    for _ in range(4):
        if not await _public_https_url(current):
            raise ImageInputError("只接受公网 HTTPS 图片地址，且不允许跳转到内网地址。")
        async with client_session(timeout=client_timeout, proxy=normalize_proxy(proxy)) as (session, request_proxy):
            async with session.get(current, proxy=request_proxy, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        break
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                if response.content_length and response.content_length > max_bytes:
                    raise ImageInputError(f"图片超过大小限制 ({max_bytes // (1024 * 1024)} MB)。")
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        raise ImageInputError(f"图片超过大小限制 ({max_bytes // (1024 * 1024)} MB)。")
                return bytes(chunks)
    raise ImageInputError("图片地址重定向次数过多。")


async def _read_component_image(
    component,
    max_bytes: int,
    timeout: int,
    proxy: str | None,
) -> bytes:
    reference = (
        getattr(component, "url", "")
        or getattr(component, "file", "")
        or getattr(component, "path", "")
        or ""
    ).strip()
    if reference.startswith(("https://", "http://")):
        data = await _download_public_image(reference, max_bytes, timeout, proxy)
        if not _is_image(data):
            raise ImageInputError("消息图片地址返回的数据不是有效图片。")
        return data
    if reference.startswith("base64://"):
        encoded = reference[len("base64://") :]
        if len(encoded) > (max_bytes * 4 // 3) + 8:
            raise ImageInputError(f"图片超过大小限制 ({max_bytes // (1024 * 1024)} MB)。")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageInputError("消息图片的 Base64 数据无效。") from exc
        if not _is_image(data):
            raise ImageInputError("消息附件不是支持的图片格式。")
        return data

    if reference.startswith("data:image/"):
        max_encoded_bytes = 4 * ((max_bytes + 2) // 3)
        if len(reference) > max_encoded_bytes + 128:
            raise ImageInputError(f"图片超过大小限制 ({max_bytes // (1024 * 1024)} MB)。")
        try:
            header, separator, encoded = reference.partition(",")
            if not separator or not header.lower().endswith(";base64"):
                raise ImageInputError("消息图片的 Data URL 无效。")
            data = base64.b64decode(encoded, validate=True)
        except ImageInputError:
            raise
        except (IndexError, binascii.Error, ValueError) as exc:
            raise ImageInputError("消息图片的 Data URL 无效。") from exc
        if len(data) > max_bytes:
            raise ImageInputError(f"图片超过大小限制 ({max_bytes // (1024 * 1024)} MB)。")
        if not _is_image(data):
            raise ImageInputError("消息附件不是支持的图片格式。")
        return data

    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

        if reference.startswith("file://"):
            parsed = urlsplit(reference)
            reference = unquote(parsed.path)
        image_path = Path(reference).resolve(strict=True)
        temp_root = Path(get_astrbot_temp_path()).resolve(strict=False)
        if temp_root not in image_path.parents:
            raise ImageInputError("只允许读取 AstrBot 临时目录内的消息附件。")
        if not image_path.is_file() or image_path.stat().st_size > max_bytes:
            raise ImageInputError(f"图片超过大小限制 ({max_bytes // (1024 * 1024)} MB) 或无法读取。")
        data = await asyncio.to_thread(image_path.read_bytes)
    except ImageInputError:
        raise
    except Exception as exc:
        raise ImageInputError(f"无法读取消息中的图片: {exc}") from exc
    if not _is_image(data):
        raise ImageInputError("消息附件不是支持的图片格式。")
    return data


async def read_image_input(
    event,
    image_url: str = "",
    max_bytes: int = 8 * 1024 * 1024,
    proxy: str | None = None,
    timeout: int = 30,
) -> bytes:
    if image_url.strip():
        data = await _download_public_image(image_url.strip(), max_bytes, timeout, proxy)
        if not _is_image(data):
            raise ImageInputError("地址返回的数据不是有效的图片。")
        return data

    from astrbot.api.message_components import Image, Reply

    message = getattr(getattr(event, "message_obj", None), "message", []) or []
    candidates = []
    for component in message:
        if isinstance(component, Image):
            candidates.append(component)
            break
    if not candidates:
        for component in message:
            if isinstance(component, Reply):
                candidates.extend(
                    item for item in component.chain or [] if isinstance(item, Image)
                )
                if candidates:
                    break
    if not candidates:
        raise ImageInputError("请附带一张图片，或回复一条含图片的消息。")
    return await _read_component_image(candidates[0], max_bytes, timeout, proxy)
