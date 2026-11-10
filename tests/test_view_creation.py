"""Use a real PostgreSQL server to exercise races between independent sessions."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, text

from ckanext.terriassistant.view_creation import ensure_default_view


@pytest.fixture
def engine():
    url = os.environ.get("TERRIASSISTANT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires an isolated PostgreSQL test database")
    engine = create_engine(url, pool_size=16, max_overflow=16)
    yield engine
    engine.dispose()


def test_concurrent_missing_views_create_once(engine):
    barrier = threading.Barrier(12)
    resource_id = str(uuid.uuid4())
    views = []
    creations = []

    def create():
        # Simulate the create action's database commit occurring after another
        # request has already read the initial empty view list.
        time.sleep(0.08)
        creations.append(resource_id)
        views.append({"view_type": "terriassistant", "id": "created"})

    def request(_):
        assert not views
        barrier.wait(timeout=10)
        return ensure_default_view(engine, resource_id, lambda: list(views), create)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(request, range(12)))
    assert creations == [resource_id]
    assert all(result == views for result in results)


def test_failure_releases_lock_without_committing_caller(engine):
    resource_id = str(uuid.uuid4())
    with engine.connect() as caller:
        transaction = caller.begin()
        caller.execute(text("CREATE TEMP TABLE caller_work (id integer)"))
        def fail():
            raise ValueError("create failed")
        with pytest.raises(ValueError, match="create failed"):
            ensure_default_view(engine, resource_id, lambda: [], fail)
        assert transaction.is_active
        views = [{"view_type": "terriassistant", "id": "another-request"}]
        assert ensure_default_view(engine, resource_id, lambda: views, fail) == views
        transaction.rollback()
