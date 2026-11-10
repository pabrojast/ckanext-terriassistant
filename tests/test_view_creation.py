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
    admin = create_engine(url)
    schema = "assistant_test_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    engine = create_engine(url, pool_size=16, max_overflow=16,
                           connect_args={"options": "-csearch_path=" + schema})
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE resource_view (id text PRIMARY KEY, "
                "resource_id text NOT NULL, view_type text NOT NULL)"))
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def insert_view(engine, resource_id):
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO resource_view VALUES (:id, :resource, 'terriassistant')"
        ), {"id": str(uuid.uuid4()), "resource": resource_id})


def test_concurrent_missing_views_create_once(engine):
    barrier = threading.Barrier(12)
    resource_id = str(uuid.uuid4())
    views = []
    creations = []

    def create():
        # Simulate the create action's database commit occurring after another
        # request has already read the initial empty view list.
        time.sleep(0.08)
        insert_view(engine, resource_id)
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
        insert_view(engine, resource_id)
        views = [{"view_type": "terriassistant", "id": "another-request"}]
        assert ensure_default_view(engine, resource_id, lambda: views, fail) == views
        transaction.rollback()


def test_hidden_existing_view_does_not_create_or_disclose_duplicate(engine):
    resource_id = str(uuid.uuid4())
    insert_view(engine, resource_id)

    def unexpected_create():
        pytest.fail("A filtered view is not a missing view")

    for _ in range(3):
        assert ensure_default_view(
            engine, resource_id, lambda: [], unexpected_create) == []
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM resource_view")).scalar() == 1


def test_concurrent_hidden_missing_view_is_created_once(engine):
    resource_id = str(uuid.uuid4())
    barrier = threading.Barrier(12)

    def request(_):
        barrier.wait(timeout=10)
        return ensure_default_view(
            engine, resource_id, lambda: [],
            lambda: insert_view(engine, resource_id))

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert list(pool.map(request, range(12))) == [[]] * 12
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM resource_view")).scalar() == 1
