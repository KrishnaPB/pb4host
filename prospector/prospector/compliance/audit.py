"""Activity log (R9)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import ActivityLog, User


def log(
    session: Session,
    action: str,
    *,
    actor: User | None = None,
    actor_label: str | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    **meta: Any,
) -> ActivityLog:
    row = ActivityLog(
        actor_id=actor.id if actor and actor.id else None,
        actor_label=actor_label or (actor.email if actor else "system"),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        meta={k: v for k, v in meta.items() if v is not None},
    )
    session.add(row)
    session.flush()
    return row
