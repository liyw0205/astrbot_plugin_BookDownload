from __future__ import annotations

from typing import Any

try:
    from astrbot.api.web import error_response, json_response, request
except ImportError:  # pragma: no cover - AstrBot supplies these at runtime
    error_response = None
    json_response = None
    request = None


PLUGIN_NAME = "astrbot_plugin_BookDownload"


class BookDownloadDashboardAPI:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    def register(self) -> None:
        context = getattr(self.plugin, "context", None)
        register_web_api = getattr(context, "register_web_api", None)
        if not callable(register_web_api):
            return
        for prefix in ("", "page/"):
            register_web_api(
                f"/{PLUGIN_NAME}/{prefix}config",
                self.get_config,
                ["GET"],
                "BookDownload get configuration",
            )
            register_web_api(
                f"/{PLUGIN_NAME}/{prefix}config",
                self.save_config,
                ["POST"],
                "BookDownload save configuration",
            )
            register_web_api(
                f"/{PLUGIN_NAME}/{prefix}health",
                self.health,
                ["GET"],
                "BookDownload health",
            )

    @staticmethod
    def _ok(data: Any) -> Any:
        payload = {"success": True, "data": data}
        if json_response is None:
            return payload
        return json_response({"data": payload})

    @staticmethod
    def _fail(message: str, status: int = 400) -> Any:
        if error_response is not None:
            return error_response(str(message), status_code=status)
        return {"success": False, "error": str(message), "data": {}}

    async def _json_object(self) -> tuple[dict[str, Any] | None, Any]:
        if request is None:
            return {}, None
        payload = await request.json(default={})
        if payload is None:
            return {}, None
        if not isinstance(payload, dict):
            return None, self._fail("请求体必须是 JSON 对象")
        return payload, None

    async def get_config(self) -> Any:
        return self._ok(self.plugin.get_config_for_web())

    async def save_config(self) -> Any:
        payload, error = await self._json_object()
        if error:
            return error
        assert payload is not None
        candidate = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        try:
            return self._ok(self.plugin.update_config_from_web(candidate))
        except Exception as exc:
            return self._fail(str(exc), 400)

    async def health(self) -> Any:
        return self._ok(self.plugin.get_health_for_web())
