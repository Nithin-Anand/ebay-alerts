import asyncio
import base64
import time

import httpx
import structlog

log = structlog.get_logger()

_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SCOPE = "https://api.ebay.com/oauth/api_scope"
# Refresh the token this many seconds before it actually expires
_EXPIRY_SKEW = 120


class EbayAuth:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self._credentials = base64.b64encode(
            f"{client_id}:{client_secret}".encode()
        ).decode()
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            return await self._fetch_token()

    async def refresh(self) -> str:
        """Force a token refresh — call after receiving a 401 from eBay."""
        async with self._lock:
            self._token = None
            self._expires_at = 0.0
            return await self._fetch_token()

    async def _fetch_token(self) -> str:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(
                _TOKEN_URL,
                headers={
                    "Authorization": f"Basic {self._credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials", "scope": _SCOPE},
            )
            resp.raise_for_status()
            data = resp.json()

        self._token = data["access_token"]
        expires_in: int = data["expires_in"]
        self._expires_at = time.monotonic() + expires_in - _EXPIRY_SKEW
        log.debug("ebay token refreshed", expires_in=expires_in)
        return self._token


async def _cli_check(client_id: str, client_secret: str) -> None:
    """Quick CLI smoke-test: python -m app.ebay.auth"""
    import sys
    auth = EbayAuth(client_id, client_secret)
    token = await auth.get_token()
    print(f"OK — token starts with: {token[:12]}...")


if __name__ == "__main__":
    import asyncio
    import sys
    from app.settings import Settings

    s = Settings()
    asyncio.run(_cli_check(s.ebay_client_id, s.ebay_client_secret))
