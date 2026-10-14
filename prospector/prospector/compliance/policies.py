"""Per-source storage policies (R3). Applied before anything from a connector is persisted."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol

from ..util import utcnow


class StoragePolicy(Protocol):
    name: str

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Return only the fields the source's terms allow us to persist."""


class PlacesStoragePolicy:
    """Google Maps Platform terms: place_id may be stored indefinitely, coordinates for up to 30
    consecutive days, nothing else. Names, phones, websites, ratings, hours are *not* returned
    here and therefore can never reach the database from this connector."""

    name = "google_places"
    ALLOWED_KEYS = ("place_id", "lat", "lng", "coords_expires_at")

    def __init__(self, coords_days: int = 30) -> None:
        self.coords_days = coords_days

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        loc = raw.get("location") or {}
        return {
            "place_id": raw["id"],
            "lat": loc.get("latitude"),
            "lng": loc.get("longitude"),
            "coords_expires_at": utcnow() + timedelta(days=self.coords_days),
        }


class PassthroughPolicy:
    """Sources whose extracted facts may be stored (business's own website, imports, manual)."""

    name = "passthrough"

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        return dict(raw)


class DropAllPolicy:
    """Connectors that must persist nothing themselves (e.g. live attribution lookups)."""

    name = "drop_all"

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {}
