"""Charles Schwab Trader API: OAuth login, token storage and authenticated requests.

Tokens are kept in a local JSON file readable only by your user (mode 0600),
outside the repository by default (``~/.optiondesk/schwab_tokens.json``).
Nothing in this module logs a token, a client secret, or the body of a
token-endpoint response.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.schwabapi.com"
AUTH_URL = f"{API_BASE}/v1/oauth/authorize"
TOKEN_URL = f"{API_BASE}/v1/oauth/token"

REFRESH_TOKEN_LIFETIME = 7 * 24 * 3600  # Schwab refresh tokens expire after 7 days
_REFRESH_MARGIN = 60  # refresh the access token this many seconds before expiry


class SchwabError(Exception):
    pass


class SchwabAuthError(SchwabError):
    """Login is missing or expired. Run ``optiondesk schwab-login``."""


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float
    refresh_obtained_at: float

    def __repr__(self) -> str:
        return f"Tokens(expires_at={self.expires_at:.0f}, <redacted>)"


class TokenStore:
    """Reads and writes tokens to a private file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Tokens | None:
        try:
            raw = json.loads(self.path.read_text())
            return Tokens(**raw)
        except FileNotFoundError:
            return None
        except (ValueError, TypeError) as exc:
            raise SchwabAuthError(f"token file {self.path} is unreadable ({type(exc).__name__})") from None

    def save(self, tokens: Tokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(asdict(tokens), fh)
        os.replace(tmp, self.path)


def code_from_redirect(redirected_url: str) -> str:
    """Pull the authorization code out of the URL Schwab redirected the browser to."""
    query = parse_qs(urlparse(redirected_url.strip()).query)
    codes = query.get("code")
    if not codes:
        raise SchwabAuthError("that URL has no ?code= parameter; copy the full address bar")
    return codes[0]


class SchwabClient:
    """Authenticated HTTP access to the Schwab API.

    Refreshes the access token automatically, retries once after a 401, and
    waits briefly on 429/503 rate limiting.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: Path,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        if not client_id or not client_secret:
            raise SchwabAuthError("set SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET in the environment")
        self._client_id = client_id
        self._client_secret = client_secret
        self.store = TokenStore(token_path)
        self._tokens = self.store.load()
        self._refresh_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(base_url=API_BASE, timeout=timeout, transport=transport)

    async def close(self) -> None:
        await self._http.aclose()

    # ── login ──

    def authorize_url(self, redirect_uri: str) -> str:
        query = urlencode(
            {"response_type": "code", "client_id": self._client_id, "redirect_uri": redirect_uri}
        )
        return f"{AUTH_URL}?{query}"

    async def exchange_code(self, code: str, redirect_uri: str) -> None:
        data = await self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        )
        now = time.time()
        self._tokens = Tokens(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=now + float(data.get("expires_in", 1800)),
            refresh_obtained_at=now,
        )
        self.store.save(self._tokens)

    # ── tokens ──

    def refresh_token_days_left(self) -> float | None:
        if self._tokens is None:
            return None
        remaining = self._tokens.refresh_obtained_at + REFRESH_TOKEN_LIFETIME - time.time()
        return remaining / 86400

    async def _access_token(self) -> str:
        if self._tokens is None:
            raise SchwabAuthError("not logged in to Schwab; run `optiondesk schwab-login`")
        if self._tokens.expires_at - time.time() < _REFRESH_MARGIN:
            await self.refresh()
        return self._tokens.access_token

    async def refresh(self) -> None:
        stale = self._tokens
        async with self._refresh_lock:
            if self._tokens is None:
                raise SchwabAuthError("not logged in to Schwab; run `optiondesk schwab-login`")
            if self._tokens is not stale:
                return  # another task refreshed while we waited
            days_left = self.refresh_token_days_left()
            if days_left is not None and days_left <= 0:
                raise SchwabAuthError(
                    "Schwab refresh token expired (7-day limit); run `optiondesk schwab-login`"
                )
            data = await self._token_request(
                {"grant_type": "refresh_token", "refresh_token": self._tokens.refresh_token}
            )
            self._tokens = Tokens(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token", self._tokens.refresh_token),
                expires_at=time.time() + float(data.get("expires_in", 1800)),
                refresh_obtained_at=self._tokens.refresh_obtained_at,
            )
            self.store.save(self._tokens)
            if days_left is not None and days_left < 1:
                log.warning(
                    "Schwab login expires in %.1f hours; run `optiondesk schwab-login` soon", days_left * 24
                )

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        try:
            resp = await self._http.post(
                TOKEN_URL,
                data=form,
                headers={"Authorization": f"Basic {basic}"},
            )
        except httpx.TransportError as exc:
            raise SchwabError(f"could not reach Schwab token endpoint ({type(exc).__name__})") from None
        if resp.status_code != 200:
            # Deliberately not logging the body: token endpoints can echo secrets.
            raise SchwabAuthError(f"Schwab token request failed with HTTP {resp.status_code}")
        try:
            data: dict[str, Any] = resp.json()
        except ValueError:
            raise SchwabAuthError("Schwab token endpoint returned malformed JSON") from None
        if not data.get("access_token"):
            raise SchwabAuthError("Schwab token response had no access_token")
        return data

    # ── requests ──

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Send an authenticated request and return the response (any status)."""
        refreshed = False
        rate_limited = False
        while True:
            token = await self._access_token()
            try:
                resp = await self._http.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            except httpx.TransportError as exc:
                raise SchwabError(f"{method} {path} failed: {type(exc).__name__}") from None
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                await self.refresh()
                continue
            if resp.status_code in (429, 503) and not rate_limited:
                rate_limited = True
                await asyncio.sleep(_retry_after(resp))
                continue
            return resp

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self.request("GET", path, params=params)
        if resp.status_code != 200:
            raise SchwabError(f"GET {path} returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError:
            raise SchwabError(f"GET {path} returned non-JSON") from None


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(1.0, min(5.0, float(resp.headers.get("Retry-After", "2"))))
    except ValueError:
        return 2.0
