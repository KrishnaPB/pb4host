"""Sign In with LinkedIn (OpenID Connect) — the only LinkedIn route that touches the API.

Scopes `openid profile email`; after the authorization-code exchange we call
`GET https://api.linkedin.com/v2/userinfo` once and keep *only* `sub`, name, e-mail and the
picture URL. The access token is discarded immediately. No profile, connections or company
endpoints are ever called (LinkedIn User Agreement §8.2; plan §4.1 route 1).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from itsdangerous import BadData, URLSafeTimedSerializer

from ..inbound.forms import Identity
from .base import Connector, ConnectorError, register

AUTHORIZATION_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
SCOPES = "openid profile email"
STATE_MAX_AGE_S = 600


@dataclass
class OidcState:
    form_key: str
    nonce: str
    return_to: str | None = None


class StateError(ConnectorError):
    pass


@register
class LinkedInOidcConnector(Connector):
    name = "linkedin_oidc"
    terms_url = "https://www.linkedin.com/legal/l/api-terms-of-use"
    requires_terms_review = True
    default_daily_cap = 500
    creates_persons = True
    person_route = "consent"  # the signed-in person consents on the form itself

    def __init__(self, settings=None, client: httpx.Client | None = None) -> None:
        super().__init__(settings)
        self._client = client

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=15.0)
        return self._client

    @property
    def configured(self) -> bool:
        return bool(self.settings.linkedin_client_id and self.settings.linkedin_client_secret)

    # --- state (signed, stateless) ---------------------------------------------------------------
    def _serializer(self) -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(self.settings.secret_key, salt="linkedin-oidc-state")

    def make_state(self, form_key: str, return_to: str | None = None) -> str:
        return self._serializer().dumps({"f": form_key, "n": secrets.token_urlsafe(16), "r": return_to})

    def read_state(self, state: str | None) -> OidcState:
        if not state:
            raise StateError("missing OAuth state")
        try:
            data = self._serializer().loads(state, max_age=STATE_MAX_AGE_S)
        except BadData as exc:
            raise StateError("OAuth state is invalid or expired") from exc
        return OidcState(form_key=data["f"], nonce=data["n"], return_to=data.get("r"))

    # --- flow --------------------------------------------------------------------------------
    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        if not self.configured:
            raise ConnectorError("LinkedIn sign-in is not configured (LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET)")
        query = urlencode({
            "response_type": "code",
            "client_id": self.settings.linkedin_client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": SCOPES,
        })
        return f"{AUTHORIZATION_URL}?{query}"

    def exchange_code(self, session, code: str, *, redirect_uri: str) -> str:
        """Authorization code → access token (never persisted)."""
        if not self.configured:
            raise ConnectorError("LinkedIn sign-in is not configured")
        self.reserve_calls(session, 1)
        try:
            resp = self.client.post(TOKEN_URL, data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.settings.linkedin_client_id,
                "client_secret": self.settings.linkedin_client_secret,
                "redirect_uri": redirect_uri,
            }, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            self.record_error(session, f"token: {exc}")
            raise ConnectorError(f"LinkedIn token exchange failed: {exc}") from exc
        if resp.status_code != 200:
            self.record_error(session, f"token HTTP {resp.status_code}")
            raise ConnectorError(f"LinkedIn token exchange failed (HTTP {resp.status_code})")
        token = resp.json().get("access_token")
        if not token:
            raise ConnectorError("LinkedIn token response had no access_token")
        return str(token)

    def userinfo(self, session, access_token: str) -> Identity:
        self.reserve_calls(session, 1)
        try:
            resp = self.client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError as exc:
            self.record_error(session, f"userinfo: {exc}")
            raise ConnectorError(f"LinkedIn userinfo failed: {exc}") from exc
        if resp.status_code != 200:
            self.record_error(session, f"userinfo HTTP {resp.status_code}")
            raise ConnectorError(f"LinkedIn userinfo failed (HTTP {resp.status_code})")
        return parse_userinfo(resp.json())


def parse_userinfo(data: dict[str, Any]) -> Identity:
    """Keep only the identity fields the plan allows: sub, name, e-mail, picture."""
    sub = data.get("sub")
    if not sub:
        raise ConnectorError("userinfo response has no subject identifier")
    name = data.get("name") or " ".join(p for p in (data.get("given_name"), data.get("family_name")) if p) or None
    return Identity(
        provider="linkedin",
        sub=str(sub),
        name=name,
        email=(data.get("email") or None),
        email_verified=bool(data.get("email_verified")),
        picture=(data.get("picture") or None),
    )
