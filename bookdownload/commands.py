from __future__ import annotations

import re

RESULT_MODES = {
    "图卡": "card",
    "card": "card",
    "图文": "image_text",
    "image_text": "image_text",
    "图文混排": "image_text",
    "文字": "text",
    "text": "text",
}


NATURAL_BOOK_SEARCH_PATTERN = re.compile(
    r"^(?:帮我\s*)?(?:搜(?:索|一下|下)?|找(?:一下|下)?|查(?:一下|下)?|来点|有没有)"
    r"\s*(?P<query>.+?)(?:的)?(?:同人本|漫画本|本子)\s*[。.!！？?，,]*$",
    re.IGNORECASE,
)


def normalize_result_mode(value: str, default: str = "card") -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        raw = str(default or "card").strip().lower()
    try:
        return RESULT_MODES[raw]
    except KeyError as exc:
        raise ValueError("搜索结果方式只能是图卡、图文或文字。") from exc


def parse_search_arguments(raw: str) -> tuple[str, int, str]:
    parts = str(raw or "").split()
    display_mode = ""
    if parts and parts[-1].lower() in RESULT_MODES:
        display_mode = RESULT_MODES[parts.pop().lower()]
    page = 1
    if parts and parts[-1].isdigit():
        page = int(parts.pop())
    return " ".join(parts).strip(), page, display_mode


def parse_natural_book_search(raw: str) -> str:
    """Extract a query from an explicit natural-language comic search request."""
    match = NATURAL_BOOK_SEARCH_PATTERN.fullmatch(str(raw or "").strip())
    if not match:
        return ""
    return re.sub(r"^[\s，,]+|[\s，,]+$", "", match.group("query"))


def has_image_component(event) -> bool:
    try:
        from astrbot.api.message_components import Image, Reply
    except ImportError:
        return False
    messages = getattr(getattr(event, "message_obj", None), "message", []) or []
    if any(isinstance(item, Image) for item in messages):
        return True
    return any(
        isinstance(item, Reply)
        and any(isinstance(quoted, Image) for quoted in (item.chain or []))
        for item in messages
    )
