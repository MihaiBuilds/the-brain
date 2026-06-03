"""
Memory Vault REST client — thin httpx wrapper around the search endpoint.

In-repo and deliberately minimal (one call per request, no pooling). A
shared client library is deferred until The Brain and several addons all
talk to Memory Vault.
"""

import httpx

# Memory Vault caps search `limit` at 50; clamp here so an over-large
# step config fails gracefully instead of returning a 422.
_MV_MAX_LIMIT = 50


class MemoryVaultError(Exception):
    """Raised when a Memory Vault request fails or returns an error status."""


class MemoryVaultClient:
    """Calls a Memory Vault instance over its REST API."""

    def __init__(self, base_url: str, token: str = "", timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    async def search(
        self,
        query: str,
        space: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Run a hybrid search and return the ranked result hits.

        Raises:
            MemoryVaultError: the request failed or returned a non-2xx status.
        """
        payload: dict = {
            "query": query,
            "limit": min(limit, _MV_MAX_LIMIT),
        }
        if space:
            payload["spaces"] = [space]

        url = f"{self._base_url}/api/search"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=payload, headers=self._headers())
                response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise MemoryVaultError(
                f"Memory Vault returned {e.response.status_code} for {url}: {e.response.text[:300]}"
            ) from e
        except httpx.HTTPError as e:
            raise MemoryVaultError(f"could not reach Memory Vault at {url}: {e}") from e

        data = response.json()
        return data.get("results", [])
