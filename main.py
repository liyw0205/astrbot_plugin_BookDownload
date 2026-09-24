"""AstrBot plugin for searching and downloading comic galleries."""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import re
from typing import Any
from urllib.parse import urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .bookdownload.commands import normalize_result_mode, parse_search_arguments, has_image_component
from .bookdownload.download import BookDownloadService, DownloadResult, normalize_download_format
from .bookdownload.details import DetailError, GalleryDetailService
from .bookdownload.detail_presentation import render_detail_card
from .bookdownload.image_input import read_image_input
from .bookdownload.models import SearchResult
from .bookdownload.network import mask_proxy, normalize_proxy
from .bookdownload.presentation import format_text_results, render_result_card
from .bookdownload.service import BookSearchService
from .webui.dashboard_api import BookDownloadDashboardAPI


MASKED_VALUE = "********"
DEFAULT_DAILY_PUSH_TAG_REGEX = "blowjob|stockings|lolicon"
LEGACY_DAILY_PUSH_TAG_REGEX = "blowjob|stockings|masturbation|lolicon"
DEFAULT_TAG_FILTER_REGEX = "yaoi|tomgirl|futanari|guro|scat|vore|bestiality"
HELP_TEXT = """本子下载指令

搜索（自动识别文字或消息/引用图片）：
  /nh搜索 <关键词|附图> [页码] [图卡|图文|文字]
  /eh搜索 <关键词|附图> [页码] [图卡|图文|文字]
  示例：/nh搜索 blue archive
  示例：回复一张图片发送 /eh搜索 图文
  图卡为默认结果样式，也可在配置页修改默认样式。
  NH 图片搜索会调用 SauceNAO，请先配置 API Key。

下载：
  /nh下载 <作品ID或完整链接> [pdf|压缩包|图片|长图]
  /eh下载 <作品ID或完整链接> [pdf|压缩包|图片|长图]
  示例：/nh下载 683646 pdf
  示例：/eh下载 https://e-hentai.org/g/123/abcdef/ 压缩包
  长图每 10 张合并一张。群聊中的图片/长图会私发给发起者。

详情：
  /nh查看 <作品ID或完整链接>
  /eh查看 <作品ID或完整链接>
  示例：/nh查看 683646
  示例：/eh查看 https://e-hentai.org/g/4209794/7e5062ea61/
  详情卡包含封面、标题、Tags、Languages、Pages、Artists、Groups 和前 6 页预览。

每日推送：
  在配置页打开每日推送开关，默认每天 12:05。
  群号和私聊/Q号分别填写，多个号码用 | 分隔；标签从正则的 | 候选中随机选择。
  /今日本子
  立即触发一次推送（每日推送开关关闭时也可手动触发），数量遵循最大结果数。

LLM 工具：book_text_search、book_image_search
配置：插件详情的“本子下载配置”页面。"""


def _today_push_success_text(result_count: int) -> str:
    """Return a user-facing acknowledgement for a completed manual push."""
    count = max(0, int(result_count))
    return f"今日份推荐已送达，共 {count} 本。"


def _today_push_empty_text() -> str:
    return "今日份推荐暂时没有找到符合条件的本子，稍后再试试吧。"


def _today_push_failed_text(result_count: int) -> str:
    count = max(0, int(result_count))
    return f"今日份推荐已生成 {count} 本，但发送失败，请稍后再试。"


class BookDownloadPlugin(Star):
    """Search supported comic sources from commands or LLM tools."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self._native_config = config
        self.config = config if isinstance(config, dict) else dict(config or {})
        self.config.setdefault("daily_push_enabled", False)
        self.config.setdefault("daily_push_time", "12:05")
        self.config.setdefault("daily_push_source", "nhentai")
        if str(self.config.get("daily_push_tag_regex", "")).strip().lower() == LEGACY_DAILY_PUSH_TAG_REGEX:
            self.config["daily_push_tag_regex"] = DEFAULT_DAILY_PUSH_TAG_REGEX
        self.config.setdefault("daily_push_tag_regex", DEFAULT_DAILY_PUSH_TAG_REGEX)
        self.config.setdefault("daily_push_language", "chinese")
        self.config.setdefault("language_filter_enabled", False)
        self.config.setdefault("tag_filter_enabled", True)
        self.config.setdefault("tag_filter_regex", DEFAULT_TAG_FILTER_REGEX)
        self.config.setdefault("daily_push_group_ids", "")
        self.config.setdefault("daily_push_friend_ids", "")
        self.config.setdefault("daily_push_platform", "aiocqhttp")
        self.config.setdefault("daily_push_target", "")
        self.service = BookSearchService(self.config)
        self.download_service = BookDownloadService(self.config)
        self.detail_service = GalleryDetailService(self.config)
        self._daily_task: asyncio.Task | None = None
        self.dashboard_api = BookDownloadDashboardAPI(self)
        try:
            self.dashboard_api.register()
        except Exception as exc:
            logger.warning("BookDownload Dashboard API registration failed: %s", exc)

    def get_config_for_web(self) -> dict[str, Any]:
        config = dict(self.config)
        for key in ("saucenao_api_key", "ehentai_cookie"):
            if str(config.get(key, "")).strip():
                config[key] = MASKED_VALUE
            else:
                config[key] = ""
        config["proxy_url"] = mask_proxy(config.get("proxy_url", ""))
        return config

    def update_config_from_web(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("配置必须是 JSON 对象。")
        next_config = dict(self.config)
        for key in ("saucenao_api_key", "ehentai_cookie"):
            if key in patch and str(patch[key]).strip() and str(patch[key]).strip() != MASKED_VALUE:
                next_config[key] = str(patch[key]).strip()
        for key in (
            "default_text_source",
            "reverse_engine",
            "ehentai_site",
            "download_format",
            "result_display_mode",
            "llm_tools_enabled",
            "daily_push_enabled",
            "daily_push_time",
            "daily_push_source",
            "daily_push_tag_regex",
            "daily_push_language",
            "language_filter_enabled",
            "tag_filter_enabled",
            "tag_filter_regex",
            "daily_push_group_ids",
            "daily_push_friend_ids",
            "daily_push_platform",
            "daily_push_target",
        ):
            if key in patch:
                next_config[key] = bool(patch[key]) if key == "daily_push_enabled" and isinstance(patch[key], bool) else patch[key]
        if "proxy_url" in patch:
            incoming_proxy = str(patch["proxy_url"] or "").strip()
            if incoming_proxy != mask_proxy(next_config.get("proxy_url", "")):
                next_config["proxy_url"] = incoming_proxy
        for key in ("timeout", "max_results", "max_image_mb", "download_max_pages", "download_max_mb"):
            if key in patch:
                try:
                    next_config[key] = int(patch[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} 必须是整数。") from exc
        if str(next_config.get("reverse_engine", "ehentai")).strip().lower() not in {"ehentai", "saucenao"}:
            raise ValueError("reverse_engine 只能是 ehentai 或 saucenao。")
        if str(next_config.get("ehentai_site", "e-hentai")).strip().lower() not in {"e-hentai", "exhentai"}:
            raise ValueError("ehentai_site 只能是 e-hentai 或 exhentai。")
        if str(next_config.get("default_text_source", "all")).strip().lower() not in {"all", "nhentai", "ehentai", "nh", "eh"}:
            raise ValueError("default_text_source 只能是 all、nhentai 或 ehentai。")
        next_config["download_format"] = normalize_download_format(next_config.get("download_format", "archive"))
        next_config["result_display_mode"] = normalize_result_mode(next_config.get("result_display_mode", "card"))
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(next_config.get("daily_push_time", "12:05")).strip()):
            raise ValueError("daily_push_time 必须是 HH:MM 格式。")
        daily_source = str(next_config.get("daily_push_source", "nhentai")).strip().lower()
        if daily_source not in {"nhentai", "ehentai", "all", "nh", "eh"}:
            raise ValueError("daily_push_source 只能是 nhentai、ehentai 或 all。")
        tag_regex = str(next_config.get("daily_push_tag_regex", "")).strip()
        if tag_regex:
            try:
                re.compile(tag_regex, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"daily_push_tag_regex 不是有效正则: {exc}") from exc
        language = str(next_config.get("daily_push_language", "")).strip()
        if language:
            try:
                re.compile(language, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"daily_push_language 不是有效正则: {exc}") from exc
        tag_filter_regex = str(next_config.get("tag_filter_regex", DEFAULT_TAG_FILTER_REGEX)).strip()
        if tag_filter_regex:
            try:
                re.compile(tag_filter_regex, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"tag_filter_regex 不是有效正则: {exc}") from exc
        if str(next_config.get("daily_push_target", "")).strip() and len(str(next_config["daily_push_target"])) > 300:
            raise ValueError("daily_push_target 过长。")
        for key in ("daily_push_group_ids", "daily_push_friend_ids"):
            target_ids = str(next_config.get(key, "")).strip()
            if target_ids and any(not item.strip().isdigit() for item in target_ids.split("|")):
                raise ValueError(f"{key} 只能填写数字，多个号码用 | 分隔。")
        normalize_proxy(next_config.get("proxy_url", ""))
        validated_service = BookSearchService(next_config)
        validated_download_service = BookDownloadService(next_config)
        validated_detail_service = GalleryDetailService(next_config)
        self.config.clear()
        self.config.update(next_config)
        native_update = getattr(self._native_config, "update", None)
        if callable(native_update) and self._native_config is not self.config:
            native_update(next_config)
        save_config = getattr(self._native_config, "save_config", None)
        if callable(save_config):
            save_config()
        self.service = validated_service
        self.download_service = validated_download_service
        self.detail_service = validated_detail_service
        self._restart_daily_task()
        return self.get_config_for_web()

    async def initialize(self) -> None:
        self._restart_daily_task()

    async def terminate(self) -> None:
        task, self._daily_task = self._daily_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _restart_daily_task(self) -> None:
        old_task, self._daily_task = self._daily_task, None
        if old_task is not None:
            old_task.cancel()
        if not self._config_bool("daily_push_enabled", False):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("Daily push task will start when AstrBot initializes the plugin")
            return
        self._daily_task = loop.create_task(self._daily_push_loop())

    def _config_bool(self, key: str, default: bool = False) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on", "启用", "开启"}

    @staticmethod
    def _seconds_until_daily(time_text: str) -> float:
        hour, minute = (int(item) for item in str(time_text or "12:05").split(":", 1))
        now = dt.datetime.now().astimezone()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        return max(1.0, (target - now).total_seconds())

    @staticmethod
    def _daily_tag_options(pattern: str) -> list[str]:
        raw = str(pattern or "").strip()
        if raw.startswith("(?:") and raw.endswith(")"):
            raw = raw[3:-1]
        options = [part.strip() for part in raw.split("|") if part.strip()]
        return options or [""]

    def _daily_target_sessions(self, event: AstrMessageEvent | None = None):
        """Build group and friend sessions from pipe-separated numeric IDs."""
        try:
            from astrbot.core.platform.message_session import MessageSession
            from astrbot.core.platform.message_type import MessageType

            platform = str(self.config.get("daily_push_platform", "")).strip()
            if not platform and event is not None:
                platform = str(event.get_platform_id() or "").strip()
            platform = platform or "aiocqhttp"
            sessions = []
            seen = set()

            def add(message_type, raw_ids: str):
                for item in str(raw_ids or "").split("|"):
                    session_id = item.strip()
                    if not session_id or not session_id.isdigit():
                        continue
                    session = MessageSession(platform, message_type, session_id)
                    key = str(session)
                    if key not in seen:
                        seen.add(key)
                        sessions.append(session)

            add(MessageType.GROUP_MESSAGE, self.config.get("daily_push_group_ids", ""))
            add(MessageType.FRIEND_MESSAGE, self.config.get("daily_push_friend_ids", ""))

            # Keep accepting the old full-session setting for existing installs.
            legacy = str(self.config.get("daily_push_target", "")).strip()
            if legacy:
                for raw in legacy.split("|"):
                    try:
                        session = MessageSession.from_str(raw.strip())
                    except Exception:
                        logger.warning("每日推送目标无效: %s", raw)
                        continue
                    if str(session) not in seen:
                        seen.add(str(session))
                        sessions.append(session)
            return sessions
        except Exception as exc:
            logger.warning("每日推送目标配置无效: %s", exc)
            return []

    async def _daily_push_loop(self) -> None:
        while True:
            await asyncio.sleep(self._seconds_until_daily(str(self.config.get("daily_push_time", "12:05"))))
            if not self._config_bool("daily_push_enabled", False):
                continue
            try:
                await self._send_daily_push()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Daily BookDownload push failed")

    async def _send_daily_push(self, event: AstrMessageEvent | None = None) -> tuple[int, int]:
        if event is not None:
            # Manual `今日本子` always replies to the invoking conversation.
            try:
                from astrbot.core.platform.message_session import MessageSession
                targets = [MessageSession(event.get_platform_id(), event.get_message_type(), event.get_session_id())]
            except Exception:
                targets = []
        else:
            targets = self._daily_target_sessions()
        if not targets:
            logger.warning("每日推送已启用，但未配置群号或私聊/Q号，跳过本次推送")
            return 0, 0
        choices = self._daily_tag_options(str(self.config.get("daily_push_tag_regex", "")))
        tag = random.choice(choices)
        language = str(self.config.get("daily_push_language", "")).strip()
        query_parts = []
        if language:
            query_parts.append(f"language:{language}")
        if tag:
            query_parts.append(tag)
        query = " ".join(query_parts) or "language:chinese"
        source = str(self.config.get("daily_push_source", "nhentai")).strip().lower()
        selected_results: list[SearchResult] = []
        seen_urls: set[str] = set()
        detail_cache: dict[str, Any] = {}
        desired_count = self.service.max_results
        for page in range(1, 6):
            results, errors = await self.service.search_text(query, source=source, page=page)
            if not results:
                if page == 1:
                    logger.warning("每日推送搜索无结果: %s; %s", query, errors)
                break
            results = await self._filter_results_by_tags(results, detail_cache)
            random.shuffle(results)
            for result in results:
                if result.url in seen_urls:
                    continue
                seen_urls.add(result.url)
                if language:
                    try:
                        candidate = detail_cache.get(result.url)
                        if candidate is None or candidate is False:
                            candidate = await self.detail_service.fetch(
                                result.url,
                                source_hint=result.source,
                                include_previews=False,
                            )
                            detail_cache[result.url] = candidate
                        if not re.search(language, " ".join(candidate.languages), re.IGNORECASE):
                            continue
                    except Exception as exc:
                        logger.debug("每日推送详情读取失败 %s: %s", result.url, exc)
                        continue
                selected_results.append(result)
                if len(selected_results) >= desired_count:
                    break
            if len(selected_results) >= desired_count:
                break
        if not selected_results:
            logger.warning("每日推送没有符合语言条件的详情: %s", query)
            return 0, 0
        # Scheduled and manual pushes use the same compact search-result card
        # as nh搜索/eh搜索; detail pages remain available through nh查看/eh查看.
        card_path = await render_result_card(
            selected_results,
            proxy=self.service.proxy,
            timeout=self.service.timeout,
        )
        sent = 0
        try:
            from astrbot.core.message.message_event_result import MessageChain
            for target in targets:
                try:
                    await self.context.send_message(target, MessageChain([Image.fromFileSystem(str(card_path))]))
                    sent += 1
                except Exception:
                    logger.exception("每日推送发送失败: %s", target)
        finally:
            asyncio.create_task(self._cleanup_path_later(card_path))
        return sent, len(selected_results)

    def get_health_for_web(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "proxy": mask_proxy(self.service.proxy or "") or "直连",
            "timeout": self.service.timeout,
            "max_results": self.service.max_results,
        }

    async def _run_text_search(self, query: str, source: str | None, page: int):
        return await self.service.search_text(query, source=source, page=page)

    async def _search_response(self, event: AstrMessageEvent, results: list[SearchResult], errors: list[str], mode: str):
        if not results:
            return event.plain_result(format_text_results(results, errors))
        if mode == "text":
            return event.plain_result(format_text_results(results, errors))
        if mode == "image_text":
            components = [Plain(format_text_results(results, errors))]
            components.extend(
                Image.fromURL(result.cover_url)
                for result in results
                if result.cover_url and self.service.is_display_image_url(result.cover_url)
            )
            return event.chain_result(components)
        card_path = await render_result_card(results, proxy=self.service.proxy, timeout=self.service.timeout)
        asyncio.create_task(self._cleanup_path_later(card_path))
        components = [Image.fromFileSystem(str(card_path))]
        if errors:
            components.append(Plain("部分来源不可用: " + "; ".join(errors)))
        return event.chain_result(components)

    @staticmethod
    async def _cleanup_path_later(path):
        await asyncio.sleep(60)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    async def _handle_search(self, event: AstrMessageEvent, source: str, raw: str):
        try:
            query, page, requested_mode = parse_search_arguments(raw)
            mode = normalize_result_mode(requested_mode, self.config.get("result_display_mode", "card"))
            if has_image_component(event):
                image = await read_image_input(
                    event,
                    max_bytes=self.service.max_image_bytes,
                    proxy=self.service.proxy,
                    timeout=self.service.timeout,
                )
                engine = "saucenao" if source == "nhentai" else "ehentai"
                results = await self.service.search_image(image, engine=engine)
                results = await self._filter_results_by_tags(results)
                results = await self._filter_results_by_language(results)
                return await self._search_response(event, results, [], mode)
            if not query:
                return event.plain_result("请提供搜索关键词，或附图/回复图片后使用搜索指令。")
            results, errors = await self._run_text_search(query, source, page)
            results = await self._filter_results_by_tags(results)
            results = await self._filter_results_by_language(results)
            return await self._search_response(event, results, errors, mode)
        except Exception as exc:
            logger.exception("BookDownload %s search failed", source)
            return event.plain_result(f"搜索失败: {exc}")

    async def _filter_results_by_language(self, results: list[SearchResult]) -> list[SearchResult]:
        """Enforce the optional language setting using each source's detail metadata."""
        if not self._config_bool("language_filter_enabled", False):
            return results
        pattern = str(self.config.get("daily_push_language", "")).strip()
        if not pattern:
            return results
        filtered: list[SearchResult] = []
        for result in results:
            try:
                hint = result.source if result.source in {"nhentai", "ehentai"} else ""
                detail = await self.detail_service.fetch(result.url, source_hint=hint, include_previews=False)
            except Exception as exc:
                logger.debug("Language filter detail request failed for %s: %s", result.url, exc)
                continue
            if re.search(pattern, " ".join(detail.languages), re.IGNORECASE):
                filtered.append(result)
        return filtered

    async def _filter_results_by_tags(
        self,
        results: list[SearchResult],
        detail_cache: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Exclude galleries whose tags match the configured regular expression."""
        if not self._config_bool("tag_filter_enabled", True):
            return results
        pattern_text = str(self.config.get("tag_filter_regex", DEFAULT_TAG_FILTER_REGEX)).strip()
        if not pattern_text:
            return results
        try:
            pattern = re.compile(pattern_text, re.IGNORECASE)
        except re.error as exc:
            logger.warning("标签过滤正则无效，跳过过滤: %s", exc)
            return results

        cache = detail_cache if detail_cache is not None else {}
        filtered: list[SearchResult] = []
        for result in results:
            tags = result.tags
            source_hint = result.source if result.source in {"nhentai", "ehentai"} else ""
            if not source_hint:
                hostname = (urlparse(result.url).hostname or "").lower()
                if hostname == "nhentai.net":
                    source_hint = "nhentai"
                elif hostname in {"e-hentai.org", "exhentai.org"}:
                    source_hint = "ehentai"
            if not tags and source_hint:
                if result.url not in cache:
                    try:
                        cache[result.url] = await self.detail_service.fetch(
                            result.url,
                            source_hint=source_hint,
                            include_previews=False,
                        )
                    except Exception as exc:
                        logger.debug("Tag filter detail request failed for %s: %s", result.url, exc)
                        cache[result.url] = False
                detail = cache[result.url]
                if detail is not False:
                    tags = detail.tags
            if pattern.search(" ".join(tags)):
                continue
            filtered.append(result)
        return filtered

    async def _send_download_result(self, event: AstrMessageEvent, result: DownloadResult) -> bool:
        """Send generated media and return whether it was sent to a group user's DM."""
        if result.format == "images":
            components = [Image.fromFileSystem(str(path)) for path in result.output_paths]
        elif result.format == "long_image":
            components = [Image.fromFileSystem(str(path)) for path in result.output_paths]
        else:
            components = [File(name=path.name, file=str(path)) for path in result.output_paths]

        try:
            if result.format in {"images", "long_image"} and not event.is_private_chat():
                sender_id = str(event.get_sender_id() or "").strip()
                platform_id = str(event.get_platform_id() or "").strip()
                if not sender_id or not platform_id:
                    raise RuntimeError("无法确定私聊目标。")
                from astrbot.core.message.message_event_result import MessageChain
                from astrbot.core.platform.message_session import MessageSession
                from astrbot.core.platform.message_type import MessageType

                target = MessageSession(platform_id, MessageType.FRIEND_MESSAGE, sender_id)
                send_message = getattr(self.context, "send_message", None)
                if not callable(send_message):
                    raise RuntimeError("当前 AstrBot 版本不支持跨会话私发。")
                sent = await send_message(target, MessageChain(components))
                if sent is False:
                    raise RuntimeError("私聊发送失败。")
                return True
            await event.send(event.chain_result(components))
            return False
        except Exception:
            result.cleanup()
            raise
        finally:
            if result.root.exists():
                asyncio.create_task(self._cleanup_download_later(result))

    @staticmethod
    async def _cleanup_download_later(result: DownloadResult) -> None:
        await asyncio.sleep(60)
        result.cleanup()

    async def _run_image_search(
        self,
        event: AstrMessageEvent,
        engine: str = "",
        image_url: str = "",
    ):
        payload = await read_image_input(
            event,
            image_url=image_url,
            max_bytes=self.service.max_image_bytes,
            proxy=self.service.proxy,
            timeout=self.service.timeout,
        )
        return await self.service.search_image(payload, engine=engine)

    @filter.command("本子帮助")
    async def book_help(self, event: AstrMessageEvent):
        """Show BookDownload commands and usage examples."""
        yield event.plain_result(HELP_TEXT)

    @filter.command("今日本子")
    async def today_book(self, event: AstrMessageEvent):
        """Trigger one immediate daily-style push."""
        try:
            sent, result_count = await self._send_daily_push(event)
            if sent:
                yield event.plain_result(_today_push_success_text(result_count))
            elif result_count:
                yield event.plain_result(_today_push_failed_text(result_count))
            else:
                yield event.plain_result(_today_push_empty_text())
        except Exception as exc:
            logger.exception("Manual BookDownload push failed")
            yield event.plain_result(f"今日份推荐生成失败：{exc}")

    @filter.command("nh搜索")
    async def nh_search(self, event: AstrMessageEvent, arguments: GreedyStr):
        yield await self._handle_search(event, "nhentai", arguments)

    @filter.command("eh搜索")
    async def eh_search(self, event: AstrMessageEvent, arguments: GreedyStr):
        yield await self._handle_search(event, "ehentai", arguments)

    @filter.command("nh下载")
    async def nh_download(self, event: AstrMessageEvent, identifier: str, output_format: str = ""):
        async for response in self._handle_download(event, "nhentai", identifier, output_format):
            yield response

    @filter.command("eh下载")
    async def eh_download(self, event: AstrMessageEvent, identifier: str, output_format: str = ""):
        async for response in self._handle_download(event, "ehentai", identifier, output_format):
            yield response

    @filter.command("nh查看")
    async def nh_view(self, event: AstrMessageEvent, arguments: GreedyStr):
        yield await self._handle_view(event, "nhentai", arguments)

    @filter.command("eh查看")
    async def eh_view(self, event: AstrMessageEvent, arguments: GreedyStr):
        yield await self._handle_view(event, "ehentai", arguments)

    async def _handle_view(self, event: AstrMessageEvent, source: str, identifier: str):
        value = str(identifier or "").strip()
        if not value:
            return event.plain_result(f"请提供作品 ID 或完整链接，例如：/{'nh' if source == 'nhentai' else 'eh'}查看 683646")
        card_path = None
        try:
            detail = await self.detail_service.fetch(value, source_hint=source)
            card_path = await render_detail_card(
                detail,
                proxy=self.detail_service.proxy,
                timeout=self.detail_service.timeout,
            )
            asyncio.create_task(self._cleanup_path_later(card_path))
            return event.chain_result([Image.fromFileSystem(str(card_path))])
        except (DetailError, ValueError) as exc:
            if card_path:
                card_path.unlink(missing_ok=True)
            return event.plain_result(f"查看失败: {exc}")
        except Exception as exc:
            if card_path:
                card_path.unlink(missing_ok=True)
            logger.exception("BookDownload %s detail failed", source)
            return event.plain_result(f"查看失败: {exc}")

    async def _handle_download(self, event: AstrMessageEvent, source: str, identifier: str, output_format: str = ""):
        """Download a gallery from an explicit NHentai or E-Hentai URL."""
        result: DownloadResult | None = None
        try:
            yield event.plain_result("正在下载本子，请稍候…")
            result = await self.download_service.download(identifier, output_format, source_hint=source)
            sent_to_dm = await self._send_download_result(event, result)
            if sent_to_dm:
                yield event.plain_result("已私发，请查收~")
            else:
                yield event.plain_result(f"下载完成：{result.title}（{len(result.output_paths)} 个文件）")
        except Exception as exc:
            if result is not None:
                result.cleanup()
            logger.exception("BookDownload gallery download failed")
            yield event.plain_result(f"下载失败: {exc}")

    @filter.llm_tool(name="book_text_search")
    async def llm_text_search(
        self,
        event: AstrMessageEvent,
        query: str,
        source: str = "",
        page: int = 1,
    ):
        """Search comic galleries from a user's natural-language request.

        Args:
            query(string): Search words or tags describing the requested comic.
            source(string): Source name: all, nhentai, or ehentai.
            page(number): Result page, starting at 1.
        """
        if not self.config.get("llm_tools_enabled", True):
            return "LLM 搜索工具已在插件配置中关闭。"
        try:
            results, errors = await self._run_text_search(query, source, page)
            results = await self._filter_results_by_tags(results)
            results = await self._filter_results_by_language(results)
            return self.service.format_results(results, errors=errors)
        except Exception as exc:
            logger.exception("BookDownload LLM text search failed")
            return f"搜索失败: {exc}"

    @filter.llm_tool(name="book_image_search")
    async def llm_image_search(
        self,
        event: AstrMessageEvent,
        engine: str = "",
        image_url: str = "",
    ):
        """Reverse-search an image attached to this message or a public image URL.

        Args:
            engine(string): Reverse engine: ehentai, saucenao, or auto.
            image_url(string): Optional public HTTPS image URL; otherwise use the message image.
        """
        if not self.config.get("llm_tools_enabled", True):
            return "LLM 搜索工具已在插件配置中关闭。"
        try:
            results = await self._run_image_search(event, engine=engine, image_url=image_url)
            results = await self._filter_results_by_tags(results)
            results = await self._filter_results_by_language(results)
            return self.service.format_results(results)
        except Exception as exc:
            logger.exception("BookDownload LLM image search failed")
            return f"反向搜图失败: {exc}"
