"""M5c-2 (ADR-0023): the operator's Redshift loader. Every check runs against moto S3 and an
in-memory stand-in for the Data API; nothing here reaches AWS.

The stand-in warehouse interprets exactly the statements the loader sends (DELETE, COPY from a full
S3 key, INSERT into the load log, the SELECT-backs), so a load really fills tables that the
SELECT-back then reads, and a test can corrupt the load to prove the loader fails closed.
"""

import io
import json
import re
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import polars as pl
import pytest
import redshift_load as rl
from db_support import three_version_store
from moto import mock_aws

from energy_curves.curves import shape
from energy_curves.curves.shape_core import CSV_HEADER, text_sha256
from energy_curves.synthetic_prices import SHAPE_SERIES, history

ACCOUNT, REGION = "123456789012", "ca-central-1"  # moto's own account
GLUE = f"ecp-glue-{ACCOUNT}-{REGION}"
DATA = f"ecp-data-{ACCOUNT}-{REGION}"
MISSING = object()
RUN = "jr_" + "a" * 64
OLDER = "jr_" + "b" * 64
SLICE = (date(2014, 1, 1), date(2014, 12, 31))
COPY_ROLE = f"arn:aws:iam::{ACCOUNT}:role/ecp-redshift-copy"


def shape_outputs() -> tuple[str, str]:
    """(csv text, hash) as the Glue job writes them, from the Polars reference on synthetic data."""
    rows = list(history(SHAPE_SERIES, *SLICE))
    frame = pl.DataFrame(
        rows,
        schema={"series_id": pl.Utf8, "observation_date": pl.Date, "price": pl.Decimal(12, 2)},
        orient="row",
    )
    result = shape.estimate_shape(frame, SLICE)
    return CSV_HEADER + shape.canonical_params_text(result.params), result.params_sha256


CSV, HASH = shape_outputs()
CSV_BYTES = CSV.rstrip("\n").encode() + b"\n"


def record_for(run_id: str = RUN, csv: str = CSV, digest: str = HASH) -> dict[str, Any]:
    return {
        "schema": 1, "run_id": run_id, "params_sha256": digest, "rows": 48,
        "outputs": {"shape_params.csv": len(csv.encode()), "shape_params.sha256": len(digest) + 1},
        "arguments": {"start": "1983-01-03", "end": "2024-04-05", "window_start": "2014-01-01",
                      "window_end": "2024-04-05", "partitions": 8},
        "completed_at": "2026-10-11T22:28:29+00:00",
    }  # fmt: skip


def put_run(
    s3: Any,
    run_id: str = RUN,
    *,
    csv: str = CSV,
    digest: str = HASH,
    record: Any = None,
    sha_text: str | None = None,
    skip: tuple[str, ...] = (),
) -> None:
    base = f"m5/shape/{run_id}"
    if "csv" not in skip:
        s3.put_object(Bucket=GLUE, Key=f"{base}/shape_params.csv/part-00000", Body=csv.encode())
    if "sha" not in skip:
        body = (sha_text if sha_text is not None else digest).encode() + b"\n"
        s3.put_object(Bucket=GLUE, Key=f"{base}/shape_params.sha256/part-00000", Body=body)
    if "record" not in skip:
        doc = record if record is not None else record_for(run_id, csv, digest)
        body = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
        s3.put_object(Bucket=GLUE, Key=f"{base}/_complete.json", Body=body)


def upload_store(s3: Any, root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            key = "store/" + path.relative_to(root).as_posix()
            s3.put_object(Bucket=DATA, Key=key, Body=path.read_bytes())


class Warehouse:
    """The Data API as a stand-in: it keeps tables, loads them with COPY from moto S3, and answers
    the loader's SELECTs. `corrupt` runs after each COPY so a test can damage the load."""

    def __init__(self, s3: Any) -> None:
        self.s3 = s3
        self.tables: dict[str, list[tuple[Any, ...]]] = {}
        self.calls: list[tuple[str, list[str]]] = []
        self.results: dict[str, list[list[dict[str, Any]]]] = {}
        self.fail_on: str | None = None
        self.after_ddl: Callable[[], None] | None = None
        self.after_copy: Callable[[str], None] | None = None
        self.corrupt: Callable[[str, list[tuple[Any, ...]]], None] | None = None
        self.sent: list[str] = []

    # the four calls the loader makes -----------------------------------------------------------
    def batch_execute_statement(
        self, *, WorkgroupName: str, Database: str, Sqls: list[str]
    ) -> dict:
        assert (WorkgroupName, Database) == ("ecp-redshift", "ecp")
        return self._run(Sqls)

    def execute_statement(self, *, WorkgroupName: str, Database: str, Sql: str) -> dict:
        assert (WorkgroupName, Database) == ("ecp-redshift", "ecp")
        return self._run([Sql])

    def describe_statement(self, *, Id: str) -> dict:
        failed = self.results.get(f"failed:{Id}")
        return {"Status": "FAILED", "Error": failed} if failed else {"Status": "FINISHED"}

    def get_statement_result(self, *, Id: str, NextToken: str | None = None) -> dict:
        return {"Records": self.results[Id]}

    # the stand-in warehouse --------------------------------------------------------------------
    def _run(self, sqls: list[str]) -> dict:
        self.sent += sqls
        self.calls.append(("batch" if len(sqls) > 1 else "one", list(sqls)))
        ident = f"id-{len(self.calls)}"
        for sql in sqls:
            if self.fail_on and self.fail_on in sql:
                self.results[f"failed:{ident}"] = "boom for account 123456789012"  # type: ignore[assignment]
                return {"Id": ident}
            self._statement(ident, sql)
        return {"Id": ident}

    def _statement(self, ident: str, sql: str) -> None:
        if sql.lstrip().upper().startswith("CREATE TABLE"):
            if self.after_ddl and "ecp_load_log" in sql:
                self.after_ddl()
            return
        if m := re.fullmatch(r"DELETE FROM public\.(\w+)", sql.strip()):
            self.tables[m[1]] = []
        elif m := re.match(r"COPY public\.(\w+) FROM '([^']+)' IAM_ROLE '([^']+)'", sql):
            self.tables[m[1]] = self._copy(m[1], m[2])
            if self.corrupt:
                self.corrupt(m[1], self.tables[m[1]])
            if self.after_copy:
                self.after_copy(m[1])
        elif sql.startswith("INSERT INTO public.ecp_load_log"):
            self.tables.setdefault("ecp_load_log", []).append((sql,))
        elif m := re.fullmatch(r"SELECT COUNT\(\*\) FROM public\.(\w+)", sql.strip()):
            self.results[ident] = [[{"longValue": len(self.tables.get(m[1], []))}]]
        elif "FROM public.ecp_shape_params" in sql:
            rows = sorted(self.tables.get("ecp_shape_params", []), key=lambda r: (r[0], r[1]))
            self.results[ident] = [
                [{"stringValue": p}, {"longValue": mo}, {"stringValue": str(s)}, {"longValue": n}]
                for p, mo, s, n in rows
            ]
        else:
            raise AssertionError(f"the loader sent a statement the stand-in does not know: {sql}")

    def _copy(self, table: str, uri: str) -> list[tuple[Any, ...]]:
        bucket, key = re.fullmatch(r"s3://([^/]+)/(.+)", uri).groups()  # type: ignore[union-attr]
        body = self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        if table == "ecp_shape_params":
            lines = body.decode().splitlines()[1:]
            return [
                (p, int(m), Decimal(s), int(n))
                for p, m, s, n in (line.split(",") for line in lines)
            ]
        return pl.read_parquet(io.BytesIO(body)).rows()


@pytest.fixture
def s3() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        for bucket in (GLUE, DATA):
            client.create_bucket(
                Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION}
            )
        yield client


@pytest.fixture
def ready(s3: Any, tmp_path: Path) -> Any:
    """One completed Glue run and a published three-version store in moto."""
    put_run(s3)
    root = three_version_store(tmp_path / "store")
    upload_store(s3, root)
    return s3


def load(s3: Any, wh: Warehouse, **kw: Any) -> rl.Summary:
    return rl.run_load(s3, wh, GLUE, DATA, COPY_ROLE, **kw)


def test_a_load_fills_the_four_tables_from_full_keys_and_logs_each(ready: Any) -> None:
    wh = Warehouse(ready)
    summary = load(ready, wh, run_id=RUN)
    assert len(wh.tables["ecp_shape_params"]) == 48
    manifest_rows = summary.rows
    for table in ("ecp_observations", "ecp_revisions", "ecp_curve_points"):
        assert len(wh.tables[table]) == manifest_rows[table] > 0, table
    assert len(wh.tables["ecp_load_log"]) == 4
    copies = [s for s in wh.sent if s.startswith("COPY")]
    assert len(copies) == 4
    for sql in copies:
        uri = re.search(r"FROM '([^']+)'", sql).group(1)  # type: ignore[union-attr]
        assert uri.endswith(("part-00000", ".parquet")) and "*" not in uri
        assert f"IAM_ROLE '{COPY_ROLE}'" in sql


def test_the_ddl_goes_first_and_each_table_is_one_delete_copy_log_transaction(ready: Any) -> None:
    wh = Warehouse(ready)
    load(ready, wh, run_id=RUN)
    kinds = [kind for kind, _ in wh.calls]
    assert wh.calls[0][0] == "batch" and all(
        s.lstrip().upper().startswith("CREATE TABLE") for s in wh.calls[0][1]
    )
    loads = [sqls for kind, sqls in wh.calls if kind == "batch" and sqls[0].startswith("DELETE")]
    assert len(loads) == 4
    for sqls in loads:
        assert [s.split()[0] for s in sqls] == ["DELETE", "COPY", "INSERT"]
    assert kinds.count("one") >= 4  # the SELECT-backs


def test_a_dry_run_checks_everything_and_sends_nothing(ready: Any) -> None:
    wh = Warehouse(ready)
    summary = load(ready, wh, run_id=RUN, dry_run=True)
    assert wh.calls == [] and summary.loaded is False and summary.run_id == RUN


def test_the_summary_and_messages_never_show_an_account_a_bucket_or_a_run_id(ready: Any) -> None:
    wh = Warehouse(ready)
    text = load(ready, wh, run_id=RUN).text()
    for secret in (ACCOUNT, GLUE, DATA, RUN):
        assert secret not in text, secret
    assert "ecp-glue-<acct>-<region>" in text and "jr_aaaaaaaa" in text


# --- the completion record: empty, unparsable or wrong means not complete ------------------------


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("empty", b""), ("whitespace", b"  \n"), ("truncated", b'{"schema": 1, "run_id": "jr_'),
        ("not json", b"not json at all"), ("null", b"null"), ("a list", b"[]"),
        ("a number", b"3"), ("a string", b'"complete"'),
    ],
)  # fmt: skip
def test_an_empty_or_unparsable_record_means_not_complete(
    ready: Any, label: str, body: bytes
) -> None:
    put_run(ready, record=body)
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused, match="not complete"):
        load(ready, wh, run_id=RUN)
    assert wh.calls == [], label


def test_a_run_without_a_record_is_not_complete(ready: Any) -> None:
    ready.delete_object(Bucket=GLUE, Key=f"m5/shape/{RUN}/_complete.json")
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused, match="no completion record"):
        load(ready, wh, run_id=RUN)
    assert wh.calls == []


def mutate(**changes: Any) -> dict[str, Any]:
    doc = record_for()
    for key, value in changes.items():
        if value is MISSING:
            del doc[key]
        else:
            doc[key] = value
    return doc


@pytest.mark.parametrize(
    "doc",
    [
        mutate(schema=2), mutate(schema="1"), mutate(schema=True), mutate(run_id=OLDER),
        mutate(run_id=MISSING), mutate(params_sha256="xyz"), mutate(params_sha256="A" * 64),
        mutate(params_sha256=HASH[:-1]), mutate(rows=47), mutate(rows="48"), mutate(rows=True),
        mutate(outputs={"shape_params.csv": 1}), mutate(outputs=[]),
        mutate(outputs={"shape_params.csv": "1091", "shape_params.sha256": 65}),
        mutate(outputs={"shape_params.csv": -1, "shape_params.sha256": 65}),
        mutate(outputs={"shape_params.csv": 1, "shape_params.sha256": 65, "x": 1}),
        {**record_for(), "extra": 1}, mutate(arguments=MISSING), mutate(completed_at=MISSING),
    ],
)  # fmt: skip
def test_a_record_with_an_unknown_shape_is_not_complete(ready: Any, doc: dict[str, Any]) -> None:
    put_run(ready, record=doc)
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused):
        load(ready, wh, run_id=RUN)
    assert wh.calls == []


# --- the objects the record describes ------------------------------------------------------------


def test_a_missing_output_object_is_refused(ready: Any) -> None:
    ready.delete_object(Bucket=GLUE, Key=f"m5/shape/{RUN}/shape_params.sha256/part-00000")
    with pytest.raises(rl.LoadRefused, match="missing"):
        load(ready, Warehouse(ready), run_id=RUN)


def test_an_output_whose_size_is_not_the_recorded_one_is_refused(ready: Any) -> None:
    put_run(ready, csv=CSV + "extra\n", record=record_for())  # the record says the original size
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused, match="size"):
        load(ready, wh, run_id=RUN)
    assert wh.calls == []


def test_the_sha256_object_must_equal_the_recorded_hash(ready: Any) -> None:
    other = "f" * 64
    put_run(ready, sha_text=other)
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused, match="sha256 object"):
        load(ready, wh, run_id=RUN)
    assert wh.calls == []


def test_the_csv_must_rebuild_the_recorded_hash_even_when_sizes_and_the_sha_object_agree(
    ready: Any,
) -> None:
    """Change one digit of one parameter: the size, the record and the .sha256 object all still
    agree, so only the second hash (the canonical text rebuilt from the CSV) can catch it."""
    lines = CSV.splitlines()
    position, month, s, n = lines[5].split(",")
    changed = s[:-1] + ("1" if s[-1] != "1" else "2")
    lines[5] = ",".join([position, month, changed, n])
    tampered = "\n".join(lines) + "\n"
    assert len(tampered) == len(CSV)
    put_run(ready, csv=tampered, record=record_for(csv=CSV))
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused, match="rebuilt"):
        load(ready, wh, run_id=RUN)
    assert wh.calls == []


def csv_without(lines_to_drop: set[int]) -> str:
    lines = CSV.splitlines()
    return "\n".join(line for i, line in enumerate(lines) if i not in lines_to_drop) + "\n"


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("47 rows", csv_without({7})),
        ("49 rows", CSV + "C1,1,0.0100000000,20\n"),
        ("duplicate key", CSV.replace("C1,2,", "C1,1,", 1)),
        ("unknown position", CSV.replace("C4,", "C5,", 1)),
        ("month 13", CSV.replace("C1,12,", "C1,13,", 1)),
        ("thin group", re.sub(r"(C2,3,[-0-9.]+),\d+", r"\1,14", CSV, count=1)),
        ("wrong header", CSV.replace("position,month,s,n_obs", "pos,month,s,n", 1)),
        ("not a number", CSV.replace("C3,5,", "C3,five,", 1)),
        ("blank line", CSV + "\n"),
    ],
)  # fmt: skip
def test_the_csv_must_be_exactly_the_48_reviewed_rows(ready: Any, label: str, text: str) -> None:
    digest = text_sha256(text.split("\n", 1)[1])  # a record and sha object that agree with it
    put_run(ready, csv=text, digest=digest)
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused):
        load(ready, wh, run_id=RUN)
    assert wh.calls == [], label


# --- which run: named, or the newest completed one ------------------------------------------------


def test_without_a_run_id_the_newest_completed_run_is_used_and_incomplete_ones_are_skipped(
    ready: Any,
) -> None:
    put_run(ready, OLDER, record={**record_for(OLDER), "completed_at": "2026-10-10T00:00:00+00:00"})
    newer_incomplete = "jr_" + "c" * 64
    put_run(ready, newer_incomplete, record=b"")  # a failed write leaves an empty file
    no_record = "jr_" + "d" * 64
    put_run(ready, no_record, skip=("record",))
    ready.put_object(Bucket=GLUE, Key="m5/shape/shape_params.csv/part-00000", Body=b"legacy")
    wh = Warehouse(ready)
    summary = load(ready, wh)
    assert summary.run_id == RUN  # completed 2026-10-11, newer than OLDER's 2026-10-10


def test_without_a_run_id_and_without_a_completed_run_nothing_is_loaded(
    s3: Any, tmp_path: Path
) -> None:
    ready_store = three_version_store(tmp_path / "store")
    upload_store(s3, ready_store)
    s3.put_object(Bucket=GLUE, Key="m5/shape/shape_params.csv/part-00000", Body=b"legacy")
    put_run(s3, RUN, record=b"")
    wh = Warehouse(s3)
    with pytest.raises(rl.LoadRefused, match="no completed"):
        load(s3, wh)
    assert wh.calls == []


@pytest.mark.parametrize("run_id", ["", "jr_", "jr_abc", "../x", "jr_" + "A" * 64, "x" * 67])
def test_a_malformed_run_id_is_refused(ready: Any, run_id: str) -> None:
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused, match="run id"):
        load(ready, wh, run_id=run_id)
    assert wh.calls == []


# --- the gold files: through current.json and the manifest ---------------------------------------


def test_the_gold_keys_come_from_the_manifest_not_from_a_listing(ready: Any) -> None:
    """A newer, unpublished run's gold files exist in the bucket; they are never used."""
    wh = Warehouse(ready)
    published = {
        k for k in (
            o["Key"] for o in ready.list_objects_v2(Bucket=DATA, Prefix="store/runs/")["Contents"]
        ) if k.endswith("gold/curves.parquet")
    }  # fmt: skip
    decoy = "store/runs/zzz-newer/attempt-9/gold/curves.parquet"
    ready.put_object(Bucket=DATA, Key=decoy, Body=b"not a published artifact")
    load(ready, wh, run_id=RUN)
    uris = [re.search(r"FROM '([^']+)'", s).group(1) for s in wh.sent if s.startswith("COPY")]  # type: ignore[union-attr]
    assert decoy not in " ".join(uris)
    assert any(u.removeprefix(f"s3://{DATA}/") in published for u in uris)


def test_no_pointer_means_nothing_is_published(s3: Any) -> None:
    put_run(s3)
    wh = Warehouse(s3)
    with pytest.raises(rl.LoadRefused, match="published"):
        load(s3, wh, run_id=RUN)
    assert wh.calls == []


def test_a_manifest_that_does_not_match_the_pointer_hash_is_refused(ready: Any) -> None:
    pointer = json.loads(
        ready.get_object(Bucket=DATA, Key="store/published/current.json")["Body"].read()
    )
    manifest_key = "store/" + pointer["manifest_key"]
    raw = ready.get_object(Bucket=DATA, Key=manifest_key)["Body"].read()
    ready.put_object(Bucket=DATA, Key=manifest_key, Body=raw.replace(b"synthetic", b"syntheti_", 1))
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused, match="manifest"):
        load(ready, wh, run_id=RUN)
    assert wh.calls == []


def manifest_of(s3: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    pointer = json.loads(
        s3.get_object(Bucket=DATA, Key="store/published/current.json")["Body"].read()
    )
    key = "store/" + pointer["manifest_key"]
    return key, pointer, json.loads(s3.get_object(Bucket=DATA, Key=key)["Body"].read())


def republish(s3: Any, key: str, pointer: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Rewrite the manifest and move the pointer's hash with it: a consistent, if wrong, store."""
    from energy_curves.pipeline.publish import dumps
    from energy_curves.storage.artifacts import sha256

    raw = dumps(manifest)
    s3.put_object(Bucket=DATA, Key=key, Body=raw)
    pointer["manifest_sha256"] = sha256(raw)
    s3.put_object(
        Bucket=DATA, Key="store/published/current.json", Body=json.dumps(pointer).encode()
    )


@pytest.mark.parametrize("name", ["gold_current", "gold_revisions", "gold_curves"])
def test_a_manifest_without_a_gold_artifact_is_refused(ready: Any, name: str) -> None:
    key, pointer, manifest = manifest_of(ready)
    del manifest["artifacts"][name]
    republish(ready, key, pointer, manifest)
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused, match=name):
        load(ready, wh, run_id=RUN)
    assert wh.calls == []


@pytest.mark.parametrize("name", ["gold_current", "gold_revisions", "gold_curves"])
def test_an_artifact_whose_bytes_do_not_match_the_manifest_hash_is_refused(
    ready: Any, name: str
) -> None:
    _, _, manifest = manifest_of(ready)
    ready.put_object(
        Bucket=DATA, Key="store/" + manifest["artifacts"][name]["key"], Body=b"PAR1 no"
    )
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused, match=name):
        load(ready, wh, run_id=RUN)
    assert wh.calls == []


@pytest.mark.parametrize(
    "bad_key",
    ["runs/x/y/silver/observations.parquet", "runs/x/gold/current.parquet",
     "../runs/x/y/gold/current.parquet", "runs/x/y/gold/curves.parquet",
     "/runs/x/y/gold/current.parquet", "runs/x/y/gold/current.parquet.bak"],
)  # fmt: skip
def test_a_gold_key_outside_the_reviewed_pattern_is_refused(ready: Any, bad_key: str) -> None:
    key, pointer, manifest = manifest_of(ready)
    manifest["artifacts"]["gold_current"]["key"] = bad_key
    republish(ready, key, pointer, manifest)
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused, match="gold_current"):
        load(ready, wh, run_id=RUN)
    assert wh.calls == []


def test_a_manifest_row_count_that_is_not_the_files_is_refused(ready: Any) -> None:
    key, pointer, manifest = manifest_of(ready)
    manifest["artifacts"]["gold_revisions"]["rows"] += 1
    republish(ready, key, pointer, manifest)
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused, match="gold_revisions"):
        load(ready, wh, run_id=RUN)
    assert wh.calls == []


def test_a_gold_file_with_other_columns_than_the_tables_is_refused(ready: Any) -> None:
    from energy_curves.pipeline.publish import parquet_bytes
    from energy_curves.storage.artifacts import sha256

    key, pointer, manifest = manifest_of(ready)
    meta = manifest["artifacts"]["gold_current"]
    frame = pl.read_parquet(
        io.BytesIO(ready.get_object(Bucket=DATA, Key="store/" + meta["key"])["Body"].read())
    )
    raw = parquet_bytes(frame.select(frame.columns[::-1]))  # same data, columns reversed
    ready.put_object(Bucket=DATA, Key="store/" + meta["key"], Body=raw)
    meta["sha256"] = sha256(raw)
    republish(ready, key, pointer, manifest)
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused, match="columns"):
        load(ready, wh, run_id=RUN)
    assert wh.calls == []


# --- exactly one listing match, with the recorded size, before each COPY --------------------------


def gold_key(s3: Any, name: str = "gold_curves") -> str:
    return "store/" + manifest_of(s3)[2]["artifacts"][name]["key"]


def test_a_sibling_that_shares_the_key_prefix_stops_the_load_before_anything_is_sent(
    ready: Any,
) -> None:
    ready.put_object(Bucket=DATA, Key=gold_key(ready) + ".bak", Body=b"x")
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused, match="exactly one"):
        load(ready, wh, run_id=RUN)
    assert wh.calls == []


def test_a_shape_sibling_also_stops_the_load(ready: Any) -> None:
    ready.put_object(Bucket=GLUE, Key=f"m5/shape/{RUN}/shape_params.csv/part-000001", Body=b"x")
    wh = Warehouse(ready)
    with pytest.raises(rl.LoadRefused, match="exactly one"):
        load(ready, wh, run_id=RUN)
    assert wh.calls == []


def test_an_object_that_changes_after_the_check_is_caught_before_its_copy_and_the_load_is_undone(
    ready: Any,
) -> None:
    wh = Warehouse(ready)
    changed = gold_key(ready, "gold_revisions")
    wh.after_ddl = lambda: ready.put_object(Bucket=DATA, Key=changed + ".bak", Body=b"x")
    with pytest.raises(rl.LoadRefused, match="exactly one"):
        load(ready, wh, run_id=RUN)
    assert all(not rows for name, rows in wh.tables.items() if name != "ecp_load_log")
    assert not any(s.startswith("COPY public.ecp_revisions") for s in wh.sent)
    assert not any(s.startswith("COPY public.ecp_curve_points") for s in wh.sent)


def test_each_copy_is_checked_again_right_before_it_runs(ready: Any) -> None:
    wh = Warehouse(ready)
    key = gold_key(ready, "gold_curves")
    wh.after_copy = lambda table: (
        ready.put_object(Bucket=DATA, Key=key + ".bak", Body=b"x")
        if table == "ecp_observations"
        else None
    )
    with pytest.raises(rl.LoadRefused, match="exactly one"):
        load(ready, wh, run_id=RUN)
    sent = [s.split(" FROM ")[0] for s in wh.sent if s.startswith("COPY")]
    assert "COPY public.ecp_curve_points" not in sent and "COPY public.ecp_observations" in sent
    assert all(not rows for name, rows in wh.tables.items() if name != "ecp_load_log")


def test_a_listed_size_that_is_not_the_recorded_one_is_refused(ready: Any) -> None:
    wh = Warehouse(ready)
    key = gold_key(ready, "gold_current")
    wh.after_ddl = lambda: ready.put_object(Bucket=DATA, Key=key, Body=b"replaced after the check")
    with pytest.raises(rl.LoadRefused, match="size"):
        load(ready, wh, run_id=RUN)
    assert not any(s.startswith("COPY") for s in wh.sent)


# --- after loading: SELECT back, and fail closed ------------------------------------------------


def test_a_shape_row_changed_in_the_warehouse_is_caught_by_the_select_back_and_all_cleared(
    ready: Any,
) -> None:
    wh = Warehouse(ready)

    def corrupt(table: str, rows: list[tuple[Any, ...]]) -> None:
        if table == "ecp_shape_params":
            p, m, s, n = rows[0]
            rows[0] = (p, m, s + Decimal("1e-10"), n)

    wh.corrupt = corrupt
    with pytest.raises(rl.PostLoadMismatch, match="hash"):
        load(ready, wh, run_id=RUN)
    assert all(not rows for name, rows in wh.tables.items() if name != "ecp_load_log")


def test_a_missing_shape_row_is_caught_by_the_select_back(ready: Any) -> None:
    wh = Warehouse(ready)
    wh.corrupt = lambda table, rows: rows.pop() if table == "ecp_shape_params" else None
    with pytest.raises(rl.PostLoadMismatch, match="48"):
        load(ready, wh, run_id=RUN)
    assert wh.tables["ecp_shape_params"] == []


@pytest.mark.parametrize("table", ["ecp_observations", "ecp_revisions", "ecp_curve_points"])
def test_a_gold_row_count_that_is_not_the_manifests_is_caught_and_cleared(
    ready: Any, table: str
) -> None:
    wh = Warehouse(ready)
    wh.corrupt = lambda t, rows: rows.pop() if t == table else None
    with pytest.raises(rl.PostLoadMismatch, match=table):
        load(ready, wh, run_id=RUN)
    assert all(not rows for name, rows in wh.tables.items() if name != "ecp_load_log")


def test_a_failed_statement_clears_what_was_loaded_and_masks_the_message(ready: Any) -> None:
    wh = Warehouse(ready)
    wh.fail_on = "COPY public.ecp_revisions"
    with pytest.raises(rl.LoadFailed) as stop:
        load(ready, wh, run_id=RUN)
    assert ACCOUNT not in str(stop.value) and "boom" in str(stop.value)
    assert all(not rows for name, rows in wh.tables.items() if name != "ecp_load_log")


# --- statement hygiene --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["a b", "a'b", "a;b", "a\nb", 'a"b', "a*b", "a%b", "", "a" * 1025, "a\\b"],
)
def test_a_key_with_an_unsafe_character_never_reaches_a_statement(key: str) -> None:
    with pytest.raises(rl.LoadRefused, match="unsafe"):
        rl.s3_uri(GLUE, key)


def test_a_bucket_other_than_the_stacks_never_reaches_a_statement() -> None:
    for bucket in ("other", f"ecp-glue-{ACCOUNT}-us-east-1", "ecp-glue-1-ca-central-1", f"{GLUE}x"):
        with pytest.raises(rl.LoadRefused, match="bucket"):
            rl.s3_uri(bucket, "m5/x")


def test_every_statement_the_loader_sends_is_known(ready: Any) -> None:
    wh = Warehouse(ready)  # the stand-in raises on any statement it does not know
    load(ready, wh, run_id=RUN)
    assert {s.split()[0] for s in wh.sent} == {"CREATE", "DELETE", "COPY", "INSERT", "SELECT"}


def test_mask_hides_accounts_bucket_names_and_run_ids() -> None:
    text = f"{ACCOUNT} {GLUE} {DATA} {RUN} arn:aws:iam::{ACCOUNT}:role/x"
    masked = rl.mask(text)
    for secret in (ACCOUNT, GLUE, DATA, RUN):
        assert secret not in masked
    assert "ecp-glue-<acct>-<region>" in masked and "ecp-data-<acct>-<region>" in masked


# --- the identity: S3 reads as the operator, statements as ecp-redshift-data-api ----------------


def test_the_data_api_client_is_built_from_an_assumed_ecp_redshift_data_api_session() -> None:
    with mock_aws():
        calls: list[dict[str, Any]] = []
        real = boto3.client("sts", region_name=REGION)
        original = real.assume_role

        class Sts:
            def get_caller_identity(self) -> dict[str, str]:
                return {"Account": ACCOUNT}

            def assume_role(self, **kw: Any) -> dict[str, Any]:
                calls.append(kw)
                return original(**kw)

        client = rl.data_api_client(Sts(), ACCOUNT, REGION)
    (call,) = calls
    assert call["RoleArn"] == f"arn:aws:iam::{ACCOUNT}:role/ecp-redshift-data-api"
    assert call["RoleSessionName"].startswith("ecp-redshift-load")
    assert call["DurationSeconds"] <= 3600
    assert client.meta.service_model.service_name == "redshift-data"
    assert client.meta.region_name == REGION


def test_main_dry_run_prints_a_masked_summary_and_exits_zero(
    ready: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert rl.main(["--dry-run", "--run-id", RUN]) == 0
    out = capsys.readouterr().out
    for secret in (ACCOUNT, GLUE, DATA, RUN):
        assert secret not in out
    assert "dry run" in out


def test_main_refuses_with_a_masked_message_and_exits_one(
    ready: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    ready.delete_object(Bucket=GLUE, Key=f"m5/shape/{RUN}/_complete.json")
    assert rl.main(["--dry-run", "--run-id", RUN]) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and ACCOUNT not in out and GLUE not in out and RUN not in out
