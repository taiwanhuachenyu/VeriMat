import json

import pytest

from src.core.events import EventValidationError
from src.orchestration.artifacts import (
    ArtifactConflict, ArtifactError, ArtifactIntegrityError, ArtifactStore,
)


def test_content_addressed_artifact_is_idempotent_and_replayable(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_json(
        tenant_id="tenant-a", job_id="job-a", logical_key="stage:plan",
        value={"queries": ["one"]},
    )
    replay = store.put_json(
        tenant_id="tenant-a", job_id="job-a", logical_key="stage:plan",
        value={"queries": ["one"]},
    )
    assert replay == ref
    assert store.read_json(ref) == {"queries": ["one"]}
    with pytest.raises(ArtifactConflict):
        store.put_json(
            tenant_id="tenant-a", job_id="job-a", logical_key="stage:plan",
            value={"queries": ["different"]},
        )


def test_a_blob_is_stored_where_windows_would_refuse_the_path(tmp_path):
    """A tenant digest, shard and content digest push an ordinary root past ``MAX_PATH``."""
    deep = tmp_path.joinpath(*("nested" + "d" * 60,) * 4)
    assert len(str(deep)) > 260
    with ArtifactStore(deep / "artifacts") as store:
        ref = store.put_bytes(
            tenant_id="tenant-a", job_id="job-a", logical_key="result",
            content=b"content", media_type="text/plain",
        )
        assert store.read_bytes(ref) == b"content"
        assert "?" not in json.dumps(ref.checkpoint_value())


def test_artifacts_are_tenant_and_job_scoped(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    store.put_bytes(
        tenant_id="tenant-a", job_id="job-a", logical_key="result",
        content=b"content", media_type="text/plain",
    )
    with pytest.raises(ArtifactError, match="not found"):
        store.get_ref(tenant_id="tenant-b", job_id="job-a", logical_key="result")
    with pytest.raises(ArtifactError, match="not found"):
        store.get_ref(tenant_id="tenant-a", job_id="job-b", logical_key="result")


def test_blob_tamper_is_detected(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_bytes(
        tenant_id="tenant", job_id="job", logical_key="result",
        content=b"content", media_type="text/plain",
    )
    store._blob_path(ref.tenant_id, ref.content_sha256).write_bytes(b"changed")
    with pytest.raises(ArtifactIntegrityError):
        store.read_bytes(ref)


def test_json_artifact_rejects_secret_keys_and_unsafe_paths(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(EventValidationError, match="sensitive fields"):
        store.put_json(
            tenant_id="tenant", job_id="job", logical_key="result",
            value={"nested": {"access_token": "forbidden"}},
        )
    with pytest.raises(ArtifactError, match="unsafe"):
        store.put_bytes(
            tenant_id="../tenant", job_id="job", logical_key="result",
            content=b"content", media_type="text/plain",
        )
