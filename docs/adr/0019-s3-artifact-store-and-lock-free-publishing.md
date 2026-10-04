# 19. M4: the S3 artifact store and publishing without a lock

Date: 2026-10-04 · Status: accepted

## Context
M4 runs the pipeline in AWS (PLAN.md M4, ADR-0003): a Lambda stages Bronze, then a Fargate task builds Silver, Gold and the curves and publishes. It runs on synthetic data only for now (user decision, 2026-10-04); real data in the cloud is a separate, later decision (PLAN.md R3, ADR-0011).

The local pipeline stays safe through three things the cloud lacks:
- **One writer at a time,** enforced by an OS file lock (`pipeline/lock.py`). The cloud has no shared filesystem to lock.
- **Overwritable run artifacts.** Files under `runs/<logical_input_id>/` could be overwritten by a retry of an unpublished input. With the lock, a retry never runs at the same time as its predecessor. In the cloud it can: a Step Functions retry or timeout does not guarantee the earlier Fargate task has stopped.
- **The version record written before the pointer.** A crash between the two left a record for a version never published, which readers ignore.

Verified on 2026-10-04:
- **S3 conditional writes** (S3 User Guide, "Conditional writes"):
  - `PutObject` with `If-None-Match: *` fails with `412` if the key exists. On versioned buckets the check is against the current version.
  - `PutObject` with `If-Match: <ETag>` fails with `412` if the object changed, and with `404` if it does not exist.
  - Of concurrent conditional writes, the first to finish wins.
- **moto 5.2.3** returns the same `412`/`412`/`404` responses, so it serves as the test stand-in. Real S3 is exercised by the first cloud run.

## Decision
1. **The store interface** gains `list(prefix)`, with S3 key-prefix semantics on both stores, and two calls for compare-and-swap:
   - `get_tagged(key) -> (bytes, tag) | None`;
   - `put_if(key, data, tag=...)`, where `tag=None` means "must not exist".

   On S3 the tag is the ETag and `put_if` is a conditional `PutObject`. Locally the tag is the content hash, and runs are still serialized by the lock. `S3ArtifactStore(client, bucket, prefix)` implements the interface, and refuses keys that would escape its prefix.
2. **The pointer write is the commit point,** and it is conditional:
   - `publish()` reads the pointer and its tag;
   - it checks, as before, that the run's base version is the published one;
   - it writes the new pointer with `If-Match` on that tag (`If-None-Match` for the first version).

   Of two runs racing to publish, exactly one commits. The other gets `PublishConflict`, and orchestration retries it from fresh state.
3. **The version index is written after the commit,** and the pointer is authoritative for its own version:
   - Every reader that enumerates versions takes versions below the pointer from the index, and the pointer's own version from the pointer. That means `published_logical_ids` and the Postgres importer's `published_versions`; the importer's rule was added after review.
   - A crash between commit and record therefore loses nothing, and the next publish writes the missing record.
   - A record at or above the pointer's version is never trusted. Stores written before this ADR recorded first, so such a record may describe a version that was never published.
4. **Run artifacts are per attempt:** `runs/<logical_input_id>/<attempt_id>/`. Two attempts of the same input never write the same key, so an attempt still running after its orchestrator gave up cannot change files a published manifest names. Manifests already record every artifact's key, so readers are unaffected, and older stores keep their layout.
5. **Two entry points:** `run_ingest(data_dir, ...)` is the local one and keeps the lock; `run_ingest_store(store, ...)` runs on any store without a lock, for the cloud.

## Consequences
- **No cloud lock service is needed** (no DynamoDB table, no lease to expire). Correctness rests on S3's conditional writes, which AWS documents and moto reproduces.
- **A losing or zombie attempt** leaves an orphaned `runs/<id>/<attempt>/` prefix. That costs storage only, and an S3 lifecycle rule can expire unpublished attempt prefixes later.
- **Tests** (`tests/test_artifact_stores.py`), run on both the local store and S3 (moto):
  - the store contract;
  - two publishers racing for one version;
  - a zombie attempt of the same input. With the old shared prefix it corrupts the published manifest (`IntegrityError`); this was confirmed by reverting the change;
  - a crash after the commit;
  - a stale record at the pointer's version.

  Each design point was checked by reverting it: the conditional write, attempt scoping, the pointer being authoritative, record repair, and ignoring records at the pointer's version.
- **One earlier expectation changed:** `test_crash_between_version_record_and_pointer_is_not_treated_as_published` asserted the old orphan record. With the record now written after the commit, no orphan is left; the test's purpose (the retry publishes, and readers see the old version until then) is unchanged.
