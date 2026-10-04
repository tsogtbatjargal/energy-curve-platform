"""ADR-0019: the artifact-store contract on both stores, and publishing without a lock.

S3 is moto 5.2.3, which follows AWS's documented conditional-write responses (checked
2026-10-04): If-None-Match on an existing key -> 412, If-Match with a stale ETag -> 412,
If-Match on a missing key -> 404. A real-S3 check runs with the first cloud execution (M4).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline import publish as publish_mod
from energy_curves.pipeline.publish import (
    POINTER_KEY,
    PublishConflict,
    load_published,
    published_logical_ids,
    read_pointer,
    version_key,
)
from energy_curves.pipeline.runner import FetchRequest, RunResult, run_ingest_store
from energy_curves.storage.artifacts import (
    ArtifactStore,
    LocalArtifactStore,
    PreconditionFailed,
    S3ArtifactStore,
)

JAN = [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 1), date(2024, 1, 31))]
FEB = [FetchRequest(("RBRTE", "RWTC"), date(2024, 2, 1), date(2024, 2, 29))]
T1 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(params=["local", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ArtifactStore]:
    if request.param == "local":
        yield LocalArtifactStore(tmp_path / "store")
        return
    with mock_aws():
        s3 = boto3.client("s3", region_name="ca-central-1")
        s3.create_bucket(
            Bucket="ecp-test-data", CreateBucketConfiguration={"LocationConstraint": "ca-central-1"}
        )
        s3.put_bucket_versioning(
            Bucket="ecp-test-data", VersioningConfiguration={"Status": "Enabled"}
        )
        yield S3ArtifactStore(s3, "ecp-test-data", prefix="batch")


def ingest(store: ArtifactStore, requests: list[FetchRequest], **kw: Any) -> RunResult:
    return run_ingest_store(
        store, SyntheticSource(retrieved_at=kw.pop("retrieved_at", T1)), requests,
        source_name="synthetic", **kw,
    )  # fmt: skip


# --- the store contract --------------------------------------------------------------------------


def test_put_get_exists_and_list(store: ArtifactStore) -> None:
    assert store.put("a/b/c.json", b"x") == store.put("a/b/c.json", b"x")  # sha256, idempotent
    store.put("a/d.json", b"y")
    store.put("ab.json", b"z")
    assert store.get("a/b/c.json") == b"x" and store.exists("a/d.json")
    assert not store.exists("a/missing.json")
    assert store.list("a/") == ["a/b/c.json", "a/d.json"]
    assert store.list("a") == ["a/b/c.json", "a/d.json", "ab.json"]  # a key prefix, as in S3
    assert store.list("nothing/") == []


def test_conditional_writes(store: ArtifactStore) -> None:
    assert store.get_tagged("p.json") is None
    store.put_if("p.json", b"1", tag=None)  # must not exist yet
    with pytest.raises(PreconditionFailed):
        store.put_if("p.json", b"2", tag=None)
    data, tag = store.get_tagged("p.json")  # type: ignore[misc]
    assert data == b"1"
    store.put_if("p.json", b"3", tag=tag)  # still the version we read
    with pytest.raises(PreconditionFailed):
        store.put_if("p.json", b"4", tag=tag)  # stale
    with pytest.raises(PreconditionFailed):
        store.put_if("other.json", b"5", tag=tag)  # a tag for an object that does not exist
    assert store.get("p.json") == b"3" and not store.exists("other.json")


@pytest.mark.parametrize("key", ["../escape.json", "a/../../escape.json"])
def test_keys_cannot_escape_the_store(store: ArtifactStore, key: str) -> None:
    with pytest.raises(ValueError, match="escapes"):
        store.put(key, b"x")


# --- publishing without a lock -------------------------------------------------------------------


def test_a_full_run_on_each_store(store: ArtifactStore) -> None:
    first = ingest(store, JAN)
    assert (first.status, first.dataset_version) == ("published", 1)
    assert ingest(store, JAN).status == "already_published"
    assert load_published(store).version == 1
    assert store.list(f"runs/{first.logical_input_id}/{first.attempt_id}/")  # attempt-scoped


def racing(store: ArtifactStore, rival: Callable[[], object]) -> ArtifactStore:
    """`store`, except that the next read of the pointer lets `rival` publish first, so the
    caller's conditional write is the one that loses."""
    real = store.get_tagged
    fired = []

    def get_tagged(key: str) -> tuple[bytes, str] | None:
        seen = real(key)
        if key == POINTER_KEY and not fired:
            fired.append(True)
            rival()
        return seen

    store.get_tagged = get_tagged  # type: ignore[method-assign]
    return store


def test_two_publishers_racing_for_the_same_version(store: ArtifactStore) -> None:
    """Both read version 0 and both pass the base-version check; the conditional pointer write
    lets only the first commit. The loser changes nothing a reader sees."""
    winner: list[RunResult] = []
    later = datetime(2026, 1, 2, tzinfo=UTC)
    racing(store, lambda: winner.append(ingest(store, FEB, retrieved_at=later)))
    with pytest.raises(PublishConflict):
        ingest(store, JAN)
    pointer = read_pointer(store)
    assert pointer is not None and pointer.logical_input_id == winner[0].logical_input_id
    assert load_published(store).version == 1  # the winner's manifest still verifies
    assert published_logical_ids(store) == {winner[0].logical_input_id: 1}


def test_a_zombie_attempt_cannot_overwrite_a_published_run(store: ArtifactStore) -> None:
    """The same input run twice at once (an orchestrator retry while the first attempt still
    runs): the retry publishes while the first attempt is still writing. The first attempt's
    later files are its own, so the published manifest and artifacts still match their hashes."""
    published: list[RunResult] = []

    def second_attempt(stage: str) -> None:
        if stage == "after_bronze" and not published:
            published.append(ingest(store, JAN))

    with pytest.raises(PublishConflict):
        ingest(store, JAN, fault=second_attempt)
    assert published[0].status == "published"
    assert load_published(store).version == 1  # IntegrityError if a manifest was overwritten


def test_a_crash_after_the_commit_still_counts_as_published(store: ArtifactStore) -> None:
    """The pointer is the commit; the version record after it is an index. If the record is
    never written, the input is still published, and the next publish repairs the record."""
    real_put = store.put

    def put(key: str, data: bytes) -> str:
        if key == version_key(1):
            raise OSError("crash after the commit")
        return real_put(key, data)

    store.put = put  # type: ignore[method-assign]
    with pytest.raises(OSError, match="after the commit"):
        ingest(store, JAN)
    store.put = real_put  # type: ignore[method-assign]
    assert not store.exists(version_key(1))
    pointer = read_pointer(store)
    assert pointer is not None and published_logical_ids(store) == {pointer.logical_input_id: 1}
    assert ingest(store, JAN).status == "already_published"
    assert ingest(store, FEB).dataset_version == 2
    assert json.loads(store.get(version_key(1)))["logical_input_id"] == pointer.logical_input_id


def test_a_stale_record_at_the_pointer_version_does_not_shadow_it(store: ArtifactStore) -> None:
    """Stores from before ADR-0019 wrote the record first; a crash could leave a record at the
    version a later run then published under another input. The pointer wins."""
    first = ingest(store, JAN)
    store.put(
        version_key(1), json.dumps({"dataset_version": 1, "logical_input_id": "stale"}).encode()
    )
    assert published_logical_ids(store) == {first.logical_input_id: 1}


def test_the_publish_module_documents_the_commit() -> None:
    assert "commit point" in (publish_mod.__doc__ or "")
