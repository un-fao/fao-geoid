"""Unit tests for import-execution dispatch (inline task / Cloud Run Jobs request)."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from geoid.config import Settings
from geoid.services import job_executor

pytestmark = pytest.mark.unit

_JOB_NAME = "projects/p/locations/europe-west1/jobs/geoid-import"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


async def test_inline_executor_schedules_run_job_and_returns_no_execution(monkeypatch):
    ran: list[str] = []

    async def _fake_run_job(job_id: str) -> int:
        ran.append(job_id)
        return 0

    from geoid.services import import_service

    monkeypatch.setattr(import_service, "run_job", _fake_run_job)
    job_id = uuid.uuid4()
    execution = await job_executor.launch(_settings(job_executor="inline"), job_id)
    assert execution is None
    await asyncio.sleep(0)  # let the scheduled task run
    assert ran == [str(job_id)]


async def test_cloud_run_executor_builds_the_exact_run_job_request(monkeypatch):
    from google.cloud import run_v2

    recorded: dict = {}

    class _FakeClient:
        def run_job(self, request):
            recorded["request"] = request
            return SimpleNamespace(metadata=SimpleNamespace(name=f"{_JOB_NAME}/executions/exec-1"))

    monkeypatch.setattr(job_executor, "_jobs_client", _FakeClient())
    settings = _settings(job_executor="cloud_run_job", import_job_name=_JOB_NAME)
    job_id = uuid.uuid4()

    execution = await job_executor.launch(settings, job_id)

    assert execution == f"{_JOB_NAME}/executions/exec-1"
    request = recorded["request"]
    assert isinstance(request, run_v2.RunJobRequest)
    assert request.name == _JOB_NAME
    override = request.overrides.container_overrides[0]
    assert [(env.name, env.value) for env in override.env] == [("GEOID_JOB_ID", str(job_id))]


async def test_cloud_run_executor_never_blocks_on_operation_result(monkeypatch):
    # operation.result() waits for the WHOLE import to finish — launch must only
    # read metadata.name. A fake whose result() explodes proves it is never called.
    class _Operation:
        metadata = SimpleNamespace(name="exec-2")

        def result(self):  # pragma: no cover - the point is it must not run
            raise AssertionError("launch must not block on operation.result()")

    class _FakeClient:
        def run_job(self, request):
            return _Operation()

    monkeypatch.setattr(job_executor, "_jobs_client", _FakeClient())
    settings = _settings(job_executor="cloud_run_job", import_job_name=_JOB_NAME)
    assert await job_executor.launch(settings, uuid.uuid4()) == "exec-2"
