from __future__ import annotations

import threading
from urllib.parse import urlsplit

ERROR_DETAIL_LIMIT = 500
RETRYABLE_STATUS_CODES = {408, 429}


class AIRequestError(RuntimeError):
    """HTTP/network failure talking to the AI endpoint.

    ``status_code`` is None for network errors (timeout, connection reset ...).
    ``retryable`` marks transient failures worth retrying with backoff.
    """

    def __init__(self, message: str, status_code: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def redact_secret(text: str, secret: str) -> str:
    text = str(text or "")
    secret = (secret or "").strip()
    if secret and len(secret) >= 4:
        text = text.replace(secret, "***")
    return text


def error_detail(text: str, secret: str, limit: int = ERROR_DETAIL_LIMIT) -> str:
    detail = redact_secret(text, secret).strip()
    if len(detail) > limit:
        detail = detail[:limit] + " ...(已截断)"
    return detail


class AITransport:
    """Reusable HTTP transport for OpenAI-compatible endpoints."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._clients: dict[str, object] = {}

    def _client_for(self, endpoint: str):
        import httpx

        parsed = urlsplit(endpoint)
        origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        with self._lock:
            client = self._clients.get(origin)
            if client is None:
                client = httpx.Client(
                    follow_redirects=False,
                    limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                    headers={"Accept": "application/json"},
                )
                self._clients[origin] = client
            return client

    def post_json(
        self,
        endpoint: str,
        payload: dict,
        api_key: str,
        timeout: int,
    ) -> dict:
        import httpx

        # 换行等非法字符会让 HTTP 库把整个请求头（含密钥，且已转义、脱敏匹配不到）写进异常。
        if not (api_key.isascii() and api_key.isprintable()):
            raise AIRequestError("API Key 含有换行、控制字符或非英文字符，请在设置页重新粘贴。请求未发送。")
        client = self._client_for(endpoint)
        try:
            response = client.post(
                endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(timeout),
            )
        except httpx.TimeoutException as exc:
            raise AIRequestError(
                f"AI API 请求超时（{timeout}s）：{error_detail(str(exc), api_key)}",
                retryable=True,
            ) from None
        except httpx.TransportError as exc:
            raise AIRequestError(
                f"AI API 网络连接失败：{error_detail(str(exc) or type(exc).__name__, api_key)}",
                retryable=True,
            ) from None
        if response.is_redirect:
            raise AIRequestError(
                f"AI API 返回了重定向 HTTP {response.status_code}，已拒绝转发密钥。",
                status_code=response.status_code,
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = response.status_code
            detail = error_detail(response.text, api_key)
            raise AIRequestError(
                f"AI API 请求失败：HTTP {status} {detail}",
                status_code=status,
                retryable=status in RETRYABLE_STATUS_CODES or status >= 500,
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("AI API 返回的不是有效 JSON。") from exc
        if not isinstance(data, dict):
            raise RuntimeError("AI API 返回的 JSON 结构无效。")
        return data

    def close(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                pass


AI_TRANSPORT = AITransport()
