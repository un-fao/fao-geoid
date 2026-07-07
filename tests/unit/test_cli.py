"""Unit tests for the CLI dispatch + alembic.ini resolution."""

from __future__ import annotations

import sys

import pytest

from geoid import cli

pytestmark = pytest.mark.unit


def test_alembic_ini_env_override(monkeypatch):
    monkeypatch.setenv("GEOID_ALEMBIC_INI", "/custom/alembic.ini")
    assert cli._alembic_ini() == "/custom/alembic.ini"


def test_alembic_ini_resolves_to_repo_file(monkeypatch):
    monkeypatch.delenv("GEOID_ALEMBIC_INI", raising=False)
    assert cli._alembic_ini().endswith("alembic.ini")


def test_main_dispatches_web(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "run_web", lambda: called.setdefault("web", True))
    monkeypatch.setattr(sys, "argv", ["geoid", "web"])
    assert cli.main() == 0
    assert called["web"] is True


def test_main_defaults_to_web(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "run_web", lambda: called.setdefault("web", True))
    monkeypatch.setattr(sys, "argv", ["geoid"])
    assert cli.main() == 0
    assert called["web"] is True


def test_main_dispatches_migrate(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "run_migrate", lambda: called.setdefault("migrate", True))
    monkeypatch.setattr(sys, "argv", ["geoid", "migrate"])
    assert cli.main() == 0
    assert called["migrate"] is True


def test_main_dispatches_import(monkeypatch):
    monkeypatch.setattr(cli, "run_import", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["geoid", "import"])
    assert cli.main() == 0


def test_main_import_propagates_worker_exit_code(monkeypatch):
    monkeypatch.setattr(cli, "run_import", lambda: 1)
    monkeypatch.setattr(sys, "argv", ["geoid", "import"])
    assert cli.main() == 1


def test_run_import_without_job_id_returns_2(monkeypatch, caplog):
    monkeypatch.delenv("GEOID_JOB_ID", raising=False)
    monkeypatch.setattr(sys, "argv", ["geoid", "import"])
    with caplog.at_level("ERROR", logger="geoid.cli"):
        assert cli.run_import() == 2
    assert "no job id" in caplog.text


async def _noop_dispose() -> None:
    return None


def test_run_import_takes_job_id_from_env(monkeypatch):
    # The Cloud Run per-execution override path: GEOID_JOB_ID reaches the worker.
    seen: list[str] = []

    async def _fake_run_job(job_id: str) -> int:
        seen.append(job_id)
        return 0

    from geoid.services import import_service

    monkeypatch.setattr(import_service, "run_job", _fake_run_job)
    monkeypatch.setattr("geoid.db.dispose_engine", _noop_dispose)
    monkeypatch.setenv("GEOID_JOB_ID", "job-uuid-from-env")
    monkeypatch.setattr(sys, "argv", ["geoid", "import"])
    assert cli.run_import() == 0
    assert seen == ["job-uuid-from-env"]


def test_run_import_takes_job_id_from_argv(monkeypatch):
    seen: list[str] = []

    async def _fake_run_job(job_id: str) -> int:
        seen.append(job_id)
        return 1

    from geoid.services import import_service

    monkeypatch.setattr(import_service, "run_job", _fake_run_job)
    monkeypatch.setattr("geoid.db.dispose_engine", _noop_dispose)
    monkeypatch.delenv("GEOID_JOB_ID", raising=False)
    monkeypatch.setattr(sys, "argv", ["geoid", "import", "job-uuid-from-argv"])
    assert cli.run_import() == 1
    assert seen == ["job-uuid-from-argv"]


def test_main_unknown_command_returns_2(monkeypatch, caplog):
    monkeypatch.setattr(sys, "argv", ["geoid", "bogus"])
    with caplog.at_level("ERROR", logger="geoid.cli"):
        assert cli.main() == 2
    assert "unknown command" in caplog.text
