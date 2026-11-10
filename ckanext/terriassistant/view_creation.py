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
        views = list_views()
        if any(isinstance(view, dict) and view.get("view_type") == "terriassistant"
               for view in views):
            return views
        create_view()
        return list_views()
