from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import aiohttp

SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
SOCKS_PROXY_SCHEMES = {"socks5", "socks5h"}


def normalize_proxy(value: Any) -> str | None:
    """Validate and normalize a proxy URL accepted by the plugin."""
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES or not parsed.hostname:
        allowed = ", ".join(sorted(SUPPORTED_PROXY_SCHEMES))
        raise ValueError(f"proxy_url 必须是 {allowed} 代理地址。")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("proxy_url 端口无效。") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("proxy_url 端口必须在 1-65535 范围内。")
    return raw


def mask_proxy(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.password is None:
        return raw
    userinfo = parsed.username or "user"
    masked_netloc = f"{userinfo}:********@{parsed.hostname}"
    if parsed.port is not None:
        masked_netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, masked_netloc, parsed.path, parsed.query, parsed.fragment))


@asynccontextmanager
async def client_session(
    *,
    timeout: aiohttp.ClientTimeout,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
) -> AsyncIterator[tuple[aiohttp.ClientSession, str | None]]:
    """Create a session and return a per-request proxy for HTTP(S) mode.

    aiohttp handles HTTP proxies directly. SOCKS proxies require the optional
    aiohttp-socks connector, while socks5h keeps DNS resolution on the proxy.
    """
    request_proxy: str | None = proxy
    connector = None
    if proxy and urlsplit(proxy).scheme.lower() in SOCKS_PROXY_SCHEMES:
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError as exc:
            raise RuntimeError("使用 socks5/socks5h 代理需要安装 aiohttp-socks。") from exc
        parsed = urlsplit(proxy)
        # Some mobile SOCKS gateways advertise ``socks5`` but only work when
        # DNS is resolved by the proxy.  Using remote DNS for both accepted
        # spellings keeps the configured proxy reliable while preserving the
        # public socks5/socks5h compatibility.
        connector_url = urlunsplit(("socks5", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
        connector = ProxyConnector.from_url(
            connector_url,
            rdns=True,
        )
        request_proxy = None
    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
        connector=connector,
    ) as session:
        yield session, request_proxy
