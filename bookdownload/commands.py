from __future__ import annotations


RESULT_MODES = {
    "图卡": "card",
    "card": "card",
    "图文": "image_text",
    "image_text": "image_text",
    "图文混排": "image_text",
    "文字": "text",
    "text": "text",
}


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
