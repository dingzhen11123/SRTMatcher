from __future__ import annotations

import threading
from urllib.parse import urlsplit


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

        client = self._client_for(endpoint)
        response = client.post(
            endpoint,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout),
        )
        if response.is_redirect:
            raise RuntimeError(
                f"AI API 返回了重定向 HTTP {response.status_code}，已拒绝转发密钥。"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:4000]
            raise RuntimeError(
                f"AI API 请求失败：HTTP {response.status_code} {detail}"
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
