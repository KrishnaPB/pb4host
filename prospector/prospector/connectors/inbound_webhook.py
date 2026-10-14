"""Signed webhook from pb4host.com's own forms → consented inbound submission.

Security model:
- the sender signs `"{timestamp}.{raw_body}"` with HMAC-SHA256 using the shared secret and sends
  `X-Prospector-Timestamp` and `X-Prospector-Signature: sha256=<hex>`;
- timestamps older than `inbound_replay_window_s` are rejected (replay protection);
- `X-Prospector-Idempotency-Key` (or `submission_id` in the body, or a body hash) makes
  re-deliveries harmless.

The plug-in snippet for the website is in docs/runbook.md ("Inbound webhook").
"""

from __future__ import annotations

import hashlib
import hmac
import time

from .base import Connector, register


class SignatureError(ValueError):
    pass


def sign(secret: str, timestamp: int | str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify_signature(secret: str | None, timestamp: str | None, signature: str | None, body: bytes,
                     *, window_s: int = 300, now: float | None = None) -> None:
    """Raise `SignatureError` unless the signature is valid and fresh."""
    if not secret:
        raise SignatureError("inbound webhook secret is not configured")
    if not timestamp or not signature:
        raise SignatureError("missing X-Prospector-Timestamp or X-Prospector-Signature")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise SignatureError("timestamp must be unix seconds") from exc
    current = now if now is not None else time.time()
    if abs(current - ts) > window_s:
        raise SignatureError("timestamp outside the replay window")
    expected = sign(secret, ts, body)
    if not hmac.compare_digest(expected, signature.strip()):
        raise SignatureError("signature mismatch")


def idempotency_key(header_value: str | None, payload: dict | None, body: bytes) -> str:
    if header_value and header_value.strip():
        return header_value.strip()[:128]
    if payload and payload.get("submission_id"):
        return str(payload["submission_id"])[:128]
    return "body:" + hashlib.sha256(body).hexdigest()[:40]


@register
class InboundWebhookConnector(Connector):
    """Not an external source: our own website posts to us. No terms review; the cap guards abuse."""

    name = "inbound_webhook"
    terms_url = ""
    requires_terms_review = False
    default_daily_cap = 2000
    creates_persons = True
    person_route = "consent"  # consent is captured with every submission (R2 exception by design)
