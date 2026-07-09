"""Contract pins for the OGC Processes v1.0 job schemas.

The statusInfo wire names/enum are 18-062r2 verbatim — these tests ARE the
contract: renaming a field (e.g. to the draft-2.0 spellings) must fail here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from geoid.schemas.job import (
    ImportFileReport,
    ImportFileSummary,
    ImportSubmission,
    StatusInfo,
    build_results,
)
from geoid.schemas.place import BulkAccepted

pytestmark = pytest.mark.unit


def test_status_info_minimal_wire_shape_is_v1_verbatim():
    info = StatusInfo(jobID="job-1", status="accepted")
    assert info.model_dump(exclude_none=True) == {
        "processID": "import",
        "type": "process",
        "jobID": "job-1",
        "status": "accepted",
    }


def test_status_info_optionals_serialize_when_present():
    info = StatusInfo(jobID="job-1", status="running", message="working", progress=40)
    dumped = info.model_dump(exclude_none=True)
    assert dumped["message"] == "working"
    assert dumped["progress"] == 40
    assert "created" not in dumped  # absent optionals are omitted, never null


@pytest.mark.parametrize("value", ["accepted", "running", "successful", "failed", "dismissed"])
def test_status_enum_accepts_all_five_v1_values(value):
    assert StatusInfo(jobID="j", status=value).status == value


def test_status_enum_rejects_non_v1_values():
    with pytest.raises(ValidationError):
        StatusInfo(jobID="j", status="succeeded")  # the draft-2.0 spelling must fail


def test_status_info_type_is_pinned_to_process():
    with pytest.raises(ValidationError):
        StatusInfo(jobID="j", status="accepted", type="job")


def test_progress_is_bounded_0_100():
    with pytest.raises(ValidationError):
        StatusInfo(jobID="j", status="running", progress=101)


def test_submission_accepts_href_alone():
    sub = ImportSubmission(href="https://storage.googleapis.com/b/o.json")
    assert sub.ref == "https://storage.googleapis.com/b/o.json"
    assert sub.is_prefix is False


def test_submission_accepts_prefix_alone():
    sub = ImportSubmission(prefix="gs://bucket/path/")
    assert sub.ref == "gs://bucket/path/"
    assert sub.is_prefix is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"href": "https://h/x", "prefix": "gs://b/p/"},
        {"href": "https://h/x", "extra": "nope"},
    ],
)
def test_submission_rejects_none_both_and_unknown_keys(payload):
    with pytest.raises(ValidationError):
        ImportSubmission.model_validate(payload)


def test_build_results_aggregates_across_files():
    files = [
        ImportFileReport(
            source="gs://b/a.geojson",
            summary=ImportFileSummary(received=3, accepted=2, rejected=1),
            accepted=[
                BulkAccepted(index=0, geoid="g", uri="u"),
                BulkAccepted(index=1, geoid="h", uri="v"),
            ],
        ),
        ImportFileReport(
            source="gs://b/b.geojson",
            summary=ImportFileSummary(received=2, accepted=0, rejected=2),
        ),
    ]
    results = build_results(files)
    assert results.summary.model_dump() == {
        "files": 2,
        "received": 5,
        "accepted": 2,
        "rejected": 3,
    }
    assert [f.source for f in results.files] == ["gs://b/a.geojson", "gs://b/b.geojson"]
