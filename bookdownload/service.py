from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .models import SearchResult
from .network import client_session, normalize_proxy
from .sources import NHENTAI_API, ehentai_search_url, nhentai_result, parse_ehentai_results

USER_AGENT = "astrbot_plugin_BookDownload/0.5.0 (https://github.com/liyw0205/astrbot_plugin_BookDownload)"
DISPLAY_IMAGE_HOSTS = (
    "nhentai.net",
    "e-hentai.org",
    "exhentai.org",
    "ehgt.org",
    "saucenao.com",
)


class BookSearchService:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.proxy = normalize_proxy(config.get("proxy_url", ""))
        self.timeout = self._bounded_int(config.get("timeout"), 30, 5, 120)
        self.max_results = self._bounded_int(config.get("max_results"), 6, 1, 12)
        self.max_image_bytes = self._bounded_int(config.get("max_image_mb"), 8, 1, 20) * 1024 * 1024

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    async def search_text(
        self,
        query: str,
        source: str | None = None,
        page: int = 1,
    ) -> tuple[list[SearchResult], list[str]]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("搜索词不能为空。")
        query = self._apply_language_filter_query(query)
        if len(query) > 200:
            raise ValueError("搜索词不能超过 200 个字符。")
        page = self._bounded_int(page, 1, 1, 50)
        selected = self._normalize_source(source or self.config.get("default_text_source", "all"))
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        }
        async with client_session(timeout=timeout, headers=headers, proxy=self.proxy) as (session, request_proxy):
            tasks = []
            if "nhentai" in selected:
                tasks.append(self._search_nhentai(session, query, page, request_proxy))
            if "ehentai" in selected:
                tasks.append(self._search_ehentai(session, query, page, request_proxy))
            responses = await asyncio.gather(*tasks, return_exceptions=True)

        source_results: list[list[SearchResult]] = []
        errors: list[str] = []
        for source_name, response in zip(selected, responses):
            if isinstance(response, Exception):
                errors.append(f"{source_name}: {response}")
            else:
                source_results.append(response)
        results: list[SearchResult] = []
        for index in range(max((len(items) for items in source_results), default=0)):
            for items in source_results:
                if index < len(items):
                    results.append(items[index])
                    if len(results) >= self.max_results:
                        return results, errors
        return results, errors

    def _language_filter_enabled(self) -> bool:
        value = self.config.get("language_filter_enabled", False)
        enabled = value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on", "开启", "启用"}
        return enabled and bool(str(self.config.get("daily_push_language", "")).strip())

    def _apply_language_filter_query(self, query: str) -> str:
        if not self._language_filter_enabled():
            return query
        language = str(self.config.get("daily_push_language", "")).strip()
        if re.search(r"(?:^|\s)language:[^\s]+", query, re.IGNORECASE):
            return query
        return f"language:{language} {query}".strip()

    @staticmethod
    def _normalize_source(source: str) -> list[str]:
        aliases = {
            "all": ["nhentai", "ehentai"],
            "nh": ["nhentai"],
            "nhentai": ["nhentai"],
            "eh": ["ehentai"],
            "e-hentai": ["ehentai"],
            "ehentai": ["ehentai"],
        }
        try:
            return aliases[str(source or "all").strip().lower()]
        except KeyError as exc:
            raise ValueError("source 只能是 all、nhentai 或 ehentai。") from exc

    async def _search_nhentai(
        self,
        session: aiohttp.ClientSession,
        query: str,
        page: int,
        request_proxy: str | None = None,
    ) -> list[SearchResult]:
        async with session.get(
            NHENTAI_API,
            params={"query": query, "page": page},
            proxy=request_proxy,
            headers={"Referer": "https://nhentai.net/"},
        ) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
        galleries = payload.get("result", []) if isinstance(payload, dict) else []
        return [
            result
            for gallery in galleries
            if isinstance(gallery, dict)
            if (result := nhentai_result(gallery)) is not None
        ]

    async def _search_ehentai(
        self,
        session: aiohttp.ClientSession,
        query: str,
        page: int,
        request_proxy: str | None = None,
    ) -> list[SearchResult]:
        site = str(self.config.get("ehentai_site", "e-hentai")).strip().lower()
        if site not in {"e-hentai", "exhentai"}:
            raise ValueError("ehentai_site 只能是 e-hentai 或 exhentai。")
        base_url = f"https://{site}.org/"
        headers = {"Referer": base_url}
        cookie = str(self.config.get("ehentai_cookie", "")).strip()
        if cookie:
            headers["Cookie"] = cookie
        async with session.get(
            ehentai_search_url(query, page, base_url),
            proxy=request_proxy,
            headers=headers,
        ) as response:
            response.raise_for_status()
            html = await response.text()
            return parse_ehentai_results(html, str(response.url))

    async def search_image(self, image: bytes, engine: str = "") -> list[SearchResult]:
        selected_engine = str(engine or self.config.get("reverse_engine", "ehentai")).strip().lower()
        if selected_engine == "auto":
            selected_engine = str(self.config.get("reverse_engine", "ehentai")).strip().lower()
        if selected_engine == "ehentai":
            return await self._reverse_ehentai(image)
        if selected_engine == "saucenao":
            return await self._reverse_saucenao(image)
        raise ValueError("engine 只能是 ehentai、saucenao 或 auto。")

    async def _reverse_ehentai(self, image: bytes) -> list[SearchResult]:
        site = str(self.config.get("ehentai_site", "e-hentai")).strip().lower()
        if site not in {"e-hentai", "exhentai"}:
            raise ValueError("ehentai_site 只能是 e-hentai 或 exhentai。")
        host = "upld.exhentai.org" if site == "exhentai" else "upld.e-hentai.org"
        endpoint = f"https://{host}/image_lookup.php"
        form = aiohttp.FormData()
        form.add_field("f_sfile", "File Search")
        form.add_field("fs_similar", "on")
        form.add_field("sfile", image, filename="search.jpg", content_type="application/octet-stream")
        headers = {"Referer": f"https://{site}.org/"}
        cookie = str(self.config.get("ehentai_cookie", "")).strip()
        if cookie:
            headers["Cookie"] = cookie
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with client_session(timeout=timeout, headers={"User-Agent": USER_AGENT}, proxy=self.proxy) as (session, request_proxy):
            async with session.post(endpoint, data=form, proxy=request_proxy, headers=headers) as response:
                response.raise_for_status()
                html = await response.text()
                return parse_ehentai_results(html, str(response.url))[: self.max_results]

    async def _reverse_saucenao(self, image: bytes) -> list[SearchResult]:
        api_key = str(self.config.get("saucenao_api_key", "")).strip()
        if not api_key:
            raise ValueError("使用 SauceNAO 需要先配置 saucenao_api_key。")
        form = aiohttp.FormData()
        form.add_field("api_key", api_key)
        form.add_field("output_type", "2")
        form.add_field("numres", str(self.max_results))
        form.add_field("file", image, filename="search.jpg", content_type="application/octet-stream")
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with client_session(timeout=timeout, headers={"User-Agent": USER_AGENT}, proxy=self.proxy) as (session, request_proxy):
            async with session.post(
                "https://saucenao.com/search.php",
                data=form,
                proxy=request_proxy,
            ) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        results: list[SearchResult] = []
        for item in payload.get("results", []):
            header = item.get("header") or {}
            data = item.get("data") or {}
            ext_urls = data.get("ext_urls") or []
            url = str(ext_urls[0]) if ext_urls else "https://saucenao.com/"
            title = next(
                (str(data[key]) for key in ("title", "material", "jp_name", "eng_name", "source") if data.get(key)),
                "(untitled)",
            )
            author_value = data.get("author") or data.get("creator") or data.get("member_name") or ""
            if isinstance(author_value, list):
                author_value = ", ".join(str(value) for value in author_value)
            results.append(
                SearchResult(
                    source="saucenao",
                    title=title,
                    url=url,
                    cover_url=str(header.get("thumbnail", "")),
                    author=str(author_value),
                    similarity=float(header.get("similarity", 0)),
                )
            )
        return results[: self.max_results]

    @staticmethod
    def is_display_image_url(url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        return any(host == domain or host.endswith("." + domain) for domain in DISPLAY_IMAGE_HOSTS)

    @staticmethod
    def format_results(
        results: list[SearchResult],
        errors: list[str] | None = None,
    ) -> str:
        if not results:
            text = "没有找到匹配结果。"
        else:
            blocks = []
            for index, result in enumerate(results, 1):
                lines = [f"[{index}] {result.title}", f"来源: {result.source}"]
                if result.author:
                    lines.append(f"作者: {result.author}")
                if result.page_count:
                    lines.append(f"页数: {result.page_count}")
                if result.similarity is not None:
                    lines.append(f"相似度: {result.similarity:g}%")
                if result.tags:
                    lines.append("标签: " + ", ".join(result.tags[:8]))
                if result.cover_url:
                    lines.append(f"封面: {result.cover_url}")
                lines.append(f"链接: {result.url}")
                blocks.append("\n".join(lines))
            text = "\n\n".join(blocks)
        if errors:
            text += "\n\n部分来源不可用: " + "; ".join(errors)
        return text
