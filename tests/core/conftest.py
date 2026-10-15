"""Fixtures for the rebuilt core: one temporary database per test."""
from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg import sql

from assistant_kernel.config import KernelSettings
from assistant_kernel.core import migrate

from .fakes import FakeHarness

TEST_DB_PREFIX = "kernel_core6_test_"


@pytest.fixture
def kernel_settings(tmp_path):
    """Settings bound to a fresh database with all core migrations applied."""
    base = KernelSettings.from_environment()
    admin = migrate.connection_args(base)
    name = TEST_DB_PREFIX + uuid.uuid4().hex
    with psycopg.connect(**admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings = base.model_copy(update={
        "postgres_database": name,
        "data_root": tmp_path,
        "provider_secrets_path": tmp_path / "provider-secrets.json",
        "shared_volcengine_path": tmp_path / "shared-models/volcengine.json",
        "service_credentials_path": tmp_path / "service-credentials.json",
    })
    try:
        migrate.apply(migrate.connection_args(settings))
        yield settings
    finally:
        assert name.startswith(TEST_DB_PREFIX)
        with psycopg.connect(**admin, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def fake_harness():
    return FakeHarness()


@pytest.fixture
async def kernel(kernel_settings):
    from assistant_kernel.core.kernel import Kernel
    k = Kernel(kernel_settings, retry_backoff_seconds=0)
    for project in ("p1", "p2"):
        await k.ensure_project(project)
    return k


@pytest.fixture
def sim(kernel):
    from .sim import Sim
    return Sim(kernel)
