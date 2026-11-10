"""Serialize automatic default views across CKAN processes and hosts."""
import hashlib

from sqlalchemy import text


def ensure_default_view(engine, resource_id, list_views, create_view):
    # A separate transaction owns only the advisory lock: the CKAN action can
    # commit its session without releasing our lock or committing caller work.
    key = int.from_bytes(hashlib.sha256(
        ("terriassistant:default-view:" + resource_id).encode("utf-8")
    ).digest()[:8], "big", signed=True)
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        # The chained list action may hide this view from the caller (for
        # example datashare's preview-only policy). Visibility is not proof
        # of absence. Check storage under the lock without returning hidden
        # rows, then obtain the response through the caller's normal filters.
        exists = connection.execute(text(
            "SELECT 1 FROM resource_view "
            "WHERE resource_id = :resource_id "
            "AND view_type = 'terriassistant' LIMIT 1"
        ), {"resource_id": resource_id}).scalar()
        if not exists:
            create_view()
        return list_views()
