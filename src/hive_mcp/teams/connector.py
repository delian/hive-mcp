"""Sending replies and looking up people through the Bot Framework connector.

A reply carries our bot's access token, so the service URL it goes to is checked: https, and a
host that belongs to the Bot Framework / Teams infrastructure. (It comes from a verified token's
claim, but a defence that costs one line is worth having.)
"""

from __future__ import annotations

from urllib.parse import quote, urlparse

import httpx

from hive_mcp.security.oauth.upstream import UpstreamError

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"  # noqa: S105 - public URL
SCOPE = "https://api.botframework.com/.default"
ALLOWED_SUFFIXES = (".botframework.com", ".botframework.us", ".trafficmanager.net")
HTTP_TIMEOUT = 10.0


def check_service_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or not host.endswith(ALLOWED_SUFFIXES):
        raise UpstreamError("refusing a service url outside the Bot Framework")
    return url.rstrip("/")


def _conversation_url(base: str, conversation: str, part: str, leaf: str) -> str:
    return f"{base}/v3/conversations/{quote(conversation, safe='')}/{part}/{quote(leaf, safe='')}"


class BotConnector:
    def __init__(
        self,
        app_id: str,
        app_password: str,
        tenant: str,
        *,
        http: httpx.AsyncClient | None = None,
        token_url: str | None = None,
    ) -> None:
        self._id, self._secret = app_id, app_password
        self._token_url = token_url or TOKEN_URL.format(tenant=tenant)
        self._http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        self._token: tuple[str, float] | None = None

    async def _access_token(self, now: float) -> str:
        if self._token and self._token[1] > now + 60:
            return self._token[0]
        try:
            response = await self._http.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials", "client_id": self._id,
                    "client_secret": self._secret, "scope": SCOPE,
                },
            )  # fmt: skip
            response.raise_for_status()
            body = response.json()
            self._token = (str(body["access_token"]), now + float(body.get("expires_in", 3600)))
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise UpstreamError("could not get a Bot Framework token") from exc
        return self._token[0]

    async def reply(
        self, service_url: str, conversation: str, activity: str, text: str, now: float
    ) -> None:
        base = check_service_url(service_url)
        url = _conversation_url(base, conversation, "activities", activity)
        try:
            await self._http.post(
                url,
                headers={"Authorization": f"Bearer {await self._access_token(now)}"},
                json={"type": "message", "text": text, "textFormat": "plain"},
            )
        except httpx.HTTPError:
            raise UpstreamError("could not send the Teams reply") from None

    async def member_email(
        self, service_url: str, conversation: str, user_id: str, now: float
    ) -> str | None:
        base = check_service_url(service_url)
        url = _conversation_url(base, conversation, "members", user_id)
        try:
            response = await self._http.get(
                url, headers={"Authorization": f"Bearer {await self._access_token(now)}"}
            )
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        email = body.get("email") or body.get("userPrincipalName")
        return str(email) if isinstance(email, str) else None
