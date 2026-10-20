"""HTTP-клиент Bot API MAX. Хост platform-api2.max.ru, токен без Bearer."""
import json

import aiohttp

from digital_professor.max_compat.ssl_context import api_ssl_context

API_BASE = "https://platform-api2.max.ru"
TEXT_LIMIT = 4000


class MaxApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"MAX API {status}: {body}")
        self.status = status
        self.body = body


class MaxClient:
    def __init__(self, token: str | None):
        self.token = token or ""
        self._session: aiohttp.ClientSession | None = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=120, sock_connect=20, sock_read=100)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def request(self, method: str, path: str, *, params=None, json_body=None):
        if not self.token:
            raise MaxApiError(0, "MAX_BOT_TOKEN is empty")
        session = await self._session_get()
        clean_params = None
        if params:
            clean_params = {k: v for k, v in params.items() if v is not None}
        async with session.request(
            method,
            API_BASE + path,
            params=clean_params,
            json=json_body,
            headers={"Authorization": self.token},
            ssl=api_ssl_context(),
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise MaxApiError(resp.status, text[:500])
            if not text:
                return {}
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()


def split_text(text: str | None) -> list[str]:
    if not text:
        return [text or ""]
    if len(text) <= TEXT_LIMIT:
        return [text]
    parts: list[str] = []
    rest = text
    while len(rest) > TEXT_LIMIT:
        cut = rest.rfind("\n", 0, TEXT_LIMIT)
        if cut < TEXT_LIMIT // 2:
            cut = TEXT_LIMIT
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        parts.append(rest)
    return parts


def clip_edit_text(text: str | None) -> str:
    text = text or ""
    if len(text) <= TEXT_LIMIT:
        return text
    return text[: TEXT_LIMIT - 20] + "\n…(обрезано)"
