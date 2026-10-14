"""EspoCRM adapter — REST API v1 with an API-key user.

Setup in EspoCRM: Administration → API Users → create a user with role access to Account and
Contact (create/read/edit), authentication method *API Key*; copy the key to ESPOCRM_API_KEY.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..connectors.base import ConnectorError
from .base import RemoteRecord


class EspoCrmAdapter:
    target = "espocrm"

    def __init__(self, base_url: str, api_key: str, *, client: httpx.Client | None = None, status_field: str = "type") -> None:
        self.base_url = base_url.rstrip("/")
        self.status_field = status_field
        self._client = client or httpx.Client(timeout=20.0, headers={"X-Api-Key": api_key, "Accept": "application/json"})

    # --- plumbing ------------------------------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self._client.request(method, self._url(path), **kwargs)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"EspoCRM request failed: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code in {401, 403}:
            raise ConnectorError(f"EspoCRM refused the API key (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise ConnectorError(f"EspoCRM HTTP {resp.status_code} for {method} {path}: {resp.text[:200]}")
        return resp.json() if resp.content else {}

    def record_url(self, entity: str, remote_id: str) -> str:
        return f"{self.base_url}/#{entity}/view/{remote_id}"

    def _to_record(self, entity: str, data: dict[str, Any]) -> RemoteRecord:
        return RemoteRecord(remote_id=str(data["id"]), url=self.record_url(entity, str(data["id"])),
                            status=(str(data.get(self.status_field)) if data.get(self.status_field) is not None else None), raw=data)

    def _search(self, entity: str, attribute: str, value: str) -> dict[str, Any] | None:
        params = {"where[0][type]": "equals", "where[0][attribute]": attribute, "where[0][value]": value, "maxSize": 5}
        data = self._request("GET", entity, params=params) or {}
        items = data.get("list") or []
        return items[0] if items else None

    # --- accounts --------------------------------------------------------------------------------
    def find_account(self, *, domain: str | None, name: str | None) -> RemoteRecord | None:
        if domain:
            for candidate in (f"https://{domain}", f"https://www.{domain}", f"http://{domain}", domain):
                found = self._search("Account", "website", candidate)
                if found:
                    return self._to_record("Account", found)
        if name:
            found = self._search("Account", "name", name)
            if found:
                return self._to_record("Account", found)
        return None

    def upsert_account(self, payload: dict[str, Any], remote_id: str | None) -> RemoteRecord:
        if remote_id:
            data = self._request("PUT", f"Account/{remote_id}", json=payload)
            if data is not None:
                return self._to_record("Account", data)
        data = self._request("POST", "Account", json=payload)
        return self._to_record("Account", data)

    def get_account(self, remote_id: str) -> RemoteRecord | None:
        data = self._request("GET", f"Account/{remote_id}")
        return self._to_record("Account", data) if data else None

    def delete_record(self, entity: str, remote_id: str) -> bool:
        """Erasure propagation: delete the CRM record (EspoCRM moves it to its recycle bin / deletes per its own policy)."""
        if entity not in {"Account", "Contact"}:
            raise ValueError(entity)
        data = self._request("DELETE", f"{entity}/{remote_id}")
        return data is not None  # None = already gone (404)

    # --- contacts --------------------------------------------------------------------------------
    def upsert_contact(self, payload: dict[str, Any], account_id: str, remote_id: str | None) -> RemoteRecord:
        body = {**payload, "accountId": account_id}
        if remote_id:
            data = self._request("PUT", f"Contact/{remote_id}", json=body)
            if data is not None:
                return self._to_record("Contact", data)
        if payload.get("emailAddress"):
            found = self._search("Contact", "emailAddress", payload["emailAddress"])
            if found:
                data = self._request("PUT", f"Contact/{found['id']}", json=body)
                return self._to_record("Contact", data or found)
        data = self._request("POST", "Contact", json=body)
        return self._to_record("Contact", data)
