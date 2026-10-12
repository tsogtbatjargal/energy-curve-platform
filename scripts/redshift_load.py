"""The operator's Redshift loader (M5c-2, ADR-0023).

    uv run python scripts/redshift_load.py [--run-id jr_...] [--dry-run] [--expected-account N]

Loads the warehouse in `ecp-redshift` with exactly what has been verified, or loads nothing:
- the Glue job's shape parameters from one completed run (`m5/shape/<run id>/`), and
- the batch store's published gold files: observations, revisions and the modelled curves.

Two identities, on purpose. S3 is read as the operator (the caller's own credentials): the record,
the sizes, the hashes and the listings. The statements go through the Data API as the role
`ecp-redshift-data-api`, which the script assumes; that role has no S3 access at all, and the COPY
itself runs as the warehouse's `ecp-redshift-copy` role. There is no password and no secret.

Every check runs before the first statement is sent, and the object checks run again right before
the load and before each COPY. A refusal sends nothing (or, if it comes after the load began,
empties the tables). The checks, in order:
1. The shape run. It is the one the operator names or else the newest whose `_complete.json` is
   valid. An empty, truncated or unparsable record means the run is NOT complete (a failed write
   on a local file system leaves an empty file), as does any record that is not exactly the
   reviewed shape. Both outputs must exist with the sizes the record states; the `.sha256` object
   must equal the recorded hash; the CSV must be exactly the 48 reviewed rows (4 positions x 12
   months, enough observations each); and the canonical text rebuilt from those rows must hash to
   the recorded hash. That is the hash twice.
2. The gold files. They come from `published/current.json` and the manifest it names, whose hash
   the pointer states and which is verified; each file's key is taken from the manifest and must
   be the reviewed `runs/<id>/<attempt>/gold/<name>.parquet`; its bytes must match the manifest's
   hash and row count, and its columns must be the table's columns in order (COPY maps by position).
3. Before each COPY, a listing with the full key as the prefix must return exactly one object
   (COPY treats its path as a key prefix, so a sibling such as `current.parquet.bak` would be
   loaded too) and its size must be the one verified.
4. After loading, a SELECT back: the shape table must hold the 48 rows and rebuild the recorded
   hash; each gold table must hold the manifest's row count. Anything else empties the tables and
   fails.

Before anything else it prints who is calling and refuses root, any account but the expected
member account (--expected-account, or terraform.tfvars) and the organization's management account,
with no S3, assume-role or Data API call made. AWS errors are printed masked, without a traceback.

Messages never show an account ID, a bucket name or a full run id.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import polars as pl

from energy_curves.curves.engine import CURVE_SCHEMA
from energy_curves.curves.shape_core import (
    CSV_HEADER,
    MIN_OBS,
    POSITIONS,
    canonical_text,
    text_sha256,
)
from energy_curves.pipeline.medallion import GOLD_SCHEMA, REVISION_SCHEMA
from energy_curves.pipeline.publish import IntegrityError, read_manifest, read_pointer
from energy_curves.storage.artifacts import S3ArtifactStore, sha256

SQL = Path(__file__).resolve().parents[1] / "sql" / "redshift"
TFVARS = Path(__file__).resolve().parents[1] / "infra" / "redshift" / "terraform.tfvars"
REGION = "ca-central-1"
WORKGROUP, DATABASE = "ecp-redshift", "ecp"
CALLER_ROLE, COPY_ROLE = "ecp-redshift-data-api", "ecp-redshift-copy"
SHAPE_PREFIX, STORE_PREFIX = "m5/shape", "store"
RUN_ID = re.compile(r"jr_[0-9a-f]{64}")
BUCKET = re.compile(r"ecp-(?:glue|data)-\d{12}-ca-central-1")
SAFE_KEY = re.compile(r"[A-Za-z0-9._/=-]{1,1024}")
SAFE_ID = re.compile(r"[A-Za-z0-9._:@=-]{1,200}")
HEX64 = re.compile(r"[0-9a-f]{64}")
RECORD_KEYS = frozenset(
    {"schema", "run_id", "params_sha256", "rows", "outputs", "arguments", "completed_at"}
)
OUTPUTS = ("shape_params.csv", "shape_params.sha256")
SHAPE_ROWS = len(POSITIONS) * 12
SHAPE_TABLE = "ecp_shape_params"
# manifest artifact -> (file name, table, the table's columns in order)
GOLD = {
    "gold_current": ("current", "ecp_observations", list(GOLD_SCHEMA)),
    "gold_revisions": ("revisions", "ecp_revisions", list(REVISION_SCHEMA)),
    "gold_curves": ("curves", "ecp_curve_points", list(CURVE_SCHEMA)),
}
TABLES = (SHAPE_TABLE, "ecp_observations", "ecp_revisions", "ecp_curve_points")
LOG_TABLE = "ecp_load_log"
COPIES = {
    SHAPE_TABLE: "copy_shape_params",
    "ecp_observations": "copy_observations",
    "ecp_revisions": "copy_revisions",
    "ecp_curve_points": "copy_curve_points",
}
URI_FIELD = {
    SHAPE_TABLE: "shape_uri",
    "ecp_observations": "observations_uri",
    "ecp_revisions": "revisions_uri",
    "ecp_curve_points": "curves_uri",
}
STATEMENT_TIMEOUT_S = 900


class LoadRefused(RuntimeError):
    """A check failed before the load: nothing was loaded."""


class PostLoadMismatch(RuntimeError):
    """What the warehouse holds after the load is not what was verified; it has been emptied."""


class LoadFailed(RuntimeError):
    """The Data API reported a failed statement; the tables have been emptied."""


def mask(text: str) -> str:
    """Account IDs, bucket names and full run ids never reach the output."""
    text = re.sub(r"ecp-(glue|data)-\d{12}-[a-z0-9-]+", r"ecp-\1-<acct>-<region>", text)
    text = re.sub(r"jr_([0-9a-f]{8})[0-9a-f]{56}", r"jr_\1…", text)
    return re.sub(r"\d{12}", "<acct>", text)


def s3_uri(bucket: str, key: str) -> str:
    """`s3://bucket/key` for a statement: this stack's buckets only, and a key of safe characters
    only (no quote, no space, no wildcard, no `..`), because the string is put into SQL."""
    if not BUCKET.fullmatch(bucket):
        raise LoadRefused("the bucket is not one of this stack's two buckets")
    if not SAFE_KEY.fullmatch(key) or key.startswith("/") or ".." in key.split("/") or "//" in key:
        raise LoadRefused("the S3 key has an unsafe character")
    return f"s3://{bucket}/{key}"


@dataclass(frozen=True)
class ShapeRun:
    run_id: str
    csv_key: str
    csv_size: int
    params_sha256: str
    rows: list[tuple[str, int, Decimal, int]]


@dataclass(frozen=True)
class GoldFile:
    name: str
    table: str
    key: str  # in the bucket, with the store prefix
    size: int
    sha256: str
    rows: int


@dataclass
class Summary:
    run_id: str
    loaded: bool
    rows: dict[str, int] = field(default_factory=dict)
    dataset_version: int = 0
    shape_sha256: str = ""

    def text(self) -> str:
        lines = [
            f"shape run {self.run_id} (hash {self.shape_sha256[:12]}…), "
            f"dataset version {self.dataset_version}, from ecp-glue-<acct>-<region> and "
            "ecp-data-<acct>-<region>",
            *(f"  {table}: {count} rows" for table, count in self.rows.items()),
            "loaded and verified by SELECT back"
            if self.loaded
            else "dry run: verified, nothing sent",
        ]
        return mask("\n".join(lines))


# --- the shape run ---------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_record(raw: bytes, run_id: str) -> dict[str, Any]:
    if not raw.strip():
        raise LoadRefused("the run is not complete: its record is empty")
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise LoadRefused("the run is not complete: its record cannot be parsed") from None
    if not isinstance(doc, dict):
        raise LoadRefused("the run is not complete: its record is not an object")
    outputs = doc.get("outputs")
    checks = [
        (set(doc) == RECORD_KEYS, "has other keys than the reviewed ones"),
        (doc.get("schema") == 1 and _is_int(doc.get("schema")), "has an unknown schema"),
        (doc.get("run_id") == run_id, "names another run"),
        (
            isinstance(doc.get("params_sha256"), str)
            and bool(HEX64.fullmatch(doc["params_sha256"])),
            "has no valid hash",
        ),
        (_is_int(doc.get("rows")) and doc.get("rows") == SHAPE_ROWS, "does not state 48 rows"),
        (
            isinstance(outputs, dict)
            and set(outputs) == set(OUTPUTS)
            and all(_is_int(v) and v >= 0 for v in outputs.values()),
            "does not state both output sizes",
        ),
        (isinstance(doc.get("arguments"), dict), "has no arguments"),
        (_iso(doc.get("completed_at")), "has no completion time"),
    ]
    for ok, problem in checks:
        if not ok:
            raise LoadRefused(f"the run is not complete: its record {problem}")
    return doc


def _iso(value: Any) -> bool:
    try:
        return isinstance(value, str) and datetime.fromisoformat(value) is not None
    except ValueError:
        return False


def _get(s3: Any, bucket: str, key: str) -> bytes | None:
    try:
        body: bytes = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return None
    return body


def read_record(s3: Any, bucket: str, run_id: str) -> dict[str, Any]:
    raw = _get(s3, bucket, f"{SHAPE_PREFIX}/{run_id}/_complete.json")
    if raw is None:
        raise LoadRefused("no completion record for this run: it is not complete")
    return parse_record(raw, run_id)


def choose_run(s3: Any, bucket: str, run_id: str | None) -> str:
    """The named run, or the newest completed one. Legacy output and runs without a valid record
    are never candidates."""
    if run_id is not None:
        if not RUN_ID.fullmatch(run_id):
            raise LoadRefused("not a Glue run id (jr_ and 64 lowercase hex digits)")
        return run_id
    found: list[tuple[str, str]] = []
    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{SHAPE_PREFIX}/", Delimiter="/"
    )
    for page in pages:
        for prefix in page.get("CommonPrefixes", []):
            candidate = prefix["Prefix"].removeprefix(f"{SHAPE_PREFIX}/").rstrip("/")
            if not RUN_ID.fullmatch(candidate):
                continue
            try:
                found.append((read_record(s3, bucket, candidate)["completed_at"], candidate))
            except LoadRefused:
                continue
    if not found:
        raise LoadRefused("no completed Glue run found under the shape prefix")
    return max(found)[1]


def parse_shape_csv(text: str) -> list[tuple[str, int, Decimal, int]]:
    if not text.endswith("\n") or "\n\n" in text:
        raise LoadRefused("the shape CSV has a blank line or no final newline")
    lines = text.split("\n")[:-1]
    if lines[:1] != [CSV_HEADER.strip()]:
        raise LoadRefused("the shape CSV header is not the reviewed one")
    rows = []
    for line in lines[1:]:
        fields = line.split(",")
        if len(fields) != 4:
            raise LoadRefused("a shape CSV row does not have 4 fields")
        position, month, s, n_obs = fields
        try:
            row = (position, int(month), Decimal(s), int(n_obs))
        except (ValueError, InvalidOperation):
            raise LoadRefused("a shape CSV row has a value that is not a number") from None
        if str(row[1]) != month or str(row[3]) != n_obs or not row[2].is_finite():
            raise LoadRefused("a shape CSV row is not in canonical form")
        rows.append(row)
    wanted = {(p, m) for p in POSITIONS for m in range(1, 13)}
    if len(rows) != SHAPE_ROWS or {(r[0], r[1]) for r in rows} != wanted:
        raise LoadRefused("the shape CSV is not exactly 4 positions x 12 months = 48 rows")
    if any(r[3] < MIN_OBS for r in rows):
        raise LoadRefused("a shape group has fewer observations than the minimum")
    return rows


def verify_shape_run(s3: Any, bucket: str, run_id: str) -> ShapeRun:
    record = read_record(s3, bucket, run_id)
    base = f"{SHAPE_PREFIX}/{run_id}"
    for name in OUTPUTS:
        key = f"{base}/{name}/part-00000"
        try:
            size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
        except s3.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                raise LoadRefused(f"the {name} output is missing") from None
            raise
        if size != record["outputs"][name]:
            raise LoadRefused(f"the {name} output has a size that is not the recorded one")
    digest = record["params_sha256"]
    sha_text = (_get(s3, bucket, f"{base}/shape_params.sha256/part-00000") or b"").decode().strip()
    if sha_text != digest:
        raise LoadRefused("the sha256 object does not equal the recorded hash")
    csv_key = f"{base}/shape_params.csv/part-00000"
    text = (_get(s3, bucket, csv_key) or b"").decode()
    rows = parse_shape_csv(text)
    if text_sha256(canonical_text(rows)) != digest:
        raise LoadRefused(
            "the canonical text rebuilt from the CSV does not hash to the recorded hash"
        )
    return ShapeRun(run_id, csv_key, record["outputs"]["shape_params.csv"], digest, rows)


# --- the gold files ------------------------------------------------------------------------------


def verify_gold(s3: Any, bucket: str) -> tuple[int, dict[str, GoldFile]]:
    store = S3ArtifactStore(s3, bucket, STORE_PREFIX)
    try:
        pointer = read_pointer(store)
    except (ValueError, KeyError, TypeError):
        raise LoadRefused("the published pointer cannot be read") from None
    if pointer is None:
        raise LoadRefused("nothing is published: there is no current.json")
    try:
        manifest = read_manifest(store, pointer)
    except IntegrityError:
        raise LoadRefused("the manifest does not match the pointer's hash") from None
    except (ValueError, KeyError, s3.exceptions.NoSuchKey):
        raise LoadRefused("the manifest cannot be read") from None
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise LoadRefused("the manifest has no artifacts")
    files = {}
    for name, (file_name, table, columns) in GOLD.items():
        meta = artifacts.get(name)
        if not isinstance(meta, dict):
            raise LoadRefused(f"{name} is not in the manifest")
        key = meta.get("key")
        if not isinstance(key, str) or not re.fullmatch(
            rf"runs/[^/]+/[^/]+/gold/{file_name}\.parquet", key
        ):
            raise LoadRefused(f"{name}: the manifest's key is not the reviewed gold path")
        try:
            raw = store.get(key)
        except s3.exceptions.NoSuchKey:
            raise LoadRefused(f"{name}: the file is missing") from None
        if sha256(raw) != meta.get("sha256"):
            raise LoadRefused(f"{name}: the bytes do not match the manifest's hash")
        try:
            frame = pl.read_parquet(io.BytesIO(raw))
        except Exception:  # noqa: BLE001 - any unreadable parquet is a refusal
            raise LoadRefused(f"{name}: the file is not readable parquet") from None
        if list(frame.columns) != columns:
            raise LoadRefused(f"{name}: the columns are not the table's columns, in order")
        if frame.height != meta.get("rows"):
            raise LoadRefused(f"{name}: the row count is not the manifest's")
        files[table] = GoldFile(
            name, table, f"{STORE_PREFIX}/{key}", len(raw), sha256(raw), frame.height
        )
    return pointer.dataset_version, files


def require_one_match(s3: Any, bucket: str, key: str, size: int, what: str) -> None:
    """COPY's path is a key prefix: the listing for the full key must return exactly that object,
    with the size that was verified."""
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=key)
    found = [(o["Key"], o["Size"]) for page in pages for o in page.get("Contents", [])]
    if [k for k, _ in found] != [key]:
        raise LoadRefused(f"{what}: the listing for the key must match exactly one object")
    if found[0][1] != size:
        raise LoadRefused(f"{what}: the object's size is not the verified one")


# --- statements and the Data API ------------------------------------------------------------


def named_sections(path: Path) -> dict[str, str]:
    parts = re.split(r"(?m)^-- name: (\w+)\s*$", path.read_text())
    return {parts[i]: parts[i + 1].strip().rstrip(";") for i in range(1, len(parts), 2)}


def ddl_statements() -> list[str]:
    text = re.sub(r"(?m)^--.*$", "", (SQL / "ddl.sql").read_text())
    return [s.strip() for s in text.split(";") if s.strip()]


def execute(data: Any, sqls: list[str], sleep: Any = time.sleep) -> str:
    """Send one statement, or several as one transaction, and wait for the result."""
    if len(sqls) == 1:
        ident = data.execute_statement(WorkgroupName=WORKGROUP, Database=DATABASE, Sql=sqls[0])[
            "Id"
        ]
    else:
        ident = data.batch_execute_statement(WorkgroupName=WORKGROUP, Database=DATABASE, Sqls=sqls)[
            "Id"
        ]
    deadline = time.monotonic() + STATEMENT_TIMEOUT_S
    while True:
        status = data.describe_statement(Id=ident)
        if status["Status"] == "FINISHED":
            return str(ident)
        if status["Status"] in {"FAILED", "ABORTED"}:
            raise LoadFailed(
                mask(f"a statement {status['Status'].lower()}: {status.get('Error', '')}")
            )
        if time.monotonic() > deadline:
            raise LoadFailed("a statement did not finish in time")
        sleep(1)


def select(data: Any, sql: str) -> list[list[Any]]:
    ident = execute(data, [sql])
    rows: list[list[Any]] = []
    token: str | None = None
    while True:
        kw = {"Id": ident, **({"NextToken": token} if token else {})}
        page = data.get_statement_result(**kw)
        for record in page["Records"]:
            rows.append([next(iter(cell.values())) for cell in record])
        token = page.get("NextToken")
        if not token:
            return rows


def load_statements(
    table: str, source_id: str, count: int, digest: str, uris: dict[str, str], role_arn: str
) -> list[str]:
    if not SAFE_ID.fullmatch(source_id) or not HEX64.fullmatch(digest):
        raise LoadRefused("an identifier in the load log is unsafe")
    copy = named_sections(SQL / "copy.sql")[COPIES[table]].format(role_arn=role_arn, **uris)
    log = (
        f"INSERT INTO public.{LOG_TABLE} (table_name, source_id, row_count, sha256) "  # noqa: S608 - table names are this file's constants
        f"VALUES ('{table}', '{source_id}', {count}, '{digest}')"
    )
    return [f"DELETE FROM public.{table}", copy, log]  # noqa: S608 - table names are this file's constants


def run_load(
    s3: Any,
    data: Any,
    glue_bucket: str,
    data_bucket: str,
    role_arn: str,
    *,
    run_id: str | None = None,
    dry_run: bool = False,
    sleep: Any = time.sleep,
) -> Summary:
    """Verify everything, then (unless `dry_run`) load. Raises LoadRefused before anything is sent;
    after the load began, any failure empties the tables first."""
    if not re.fullmatch(r"arn:aws:iam::\d{12}:role/ecp-redshift-copy", role_arn):
        raise LoadRefused("the COPY role is not ecp-redshift-copy")
    run = choose_run(s3, glue_bucket, run_id)
    shape = verify_shape_run(s3, glue_bucket, run)
    version, gold = verify_gold(s3, data_bucket)
    sources = {SHAPE_TABLE: (glue_bucket, shape.csv_key, shape.csv_size, "the shape CSV")}
    sources |= {t: (data_bucket, g.key, g.size, g.name) for t, g in gold.items()}

    def recheck() -> None:
        for bucket, key, size, what in sources.values():
            require_one_match(s3, bucket, key, size, what)

    recheck()
    counts = {SHAPE_TABLE: SHAPE_ROWS, **{t: g.rows for t, g in gold.items()}}
    summary = Summary(run, False, counts, version, shape.params_sha256)
    if dry_run:
        return summary
    uris = {URI_FIELD[t]: s3_uri(bucket, key) for t, (bucket, key, _, _) in sources.items()}
    digests = {SHAPE_TABLE: shape.params_sha256, **{t: g.sha256 for t, g in gold.items()}}
    ids = {SHAPE_TABLE: run, **{t: f"{g.name}@v{version}" for t, g in gold.items()}}
    plans = {t: load_statements(t, ids[t], counts[t], digests[t], uris, role_arn) for t in TABLES}
    try:
        execute(data, ddl_statements(), sleep)
        recheck()  # the objects are checked again right before the first statement that reads them
        execute(data, [f"DELETE FROM public.{LOG_TABLE}"], sleep)  # noqa: S608 - table names are this file's constants
        for table in TABLES:
            bucket, key, size, what = sources[table]
            require_one_match(s3, bucket, key, size, what)
            execute(data, plans[table], sleep)
        verify_after(data, shape, counts, sleep)
    except (LoadRefused, LoadFailed, PostLoadMismatch):
        empty(data, sleep)
        raise
    summary.loaded = True
    return summary


def empty(data: Any, sleep: Any) -> None:
    """Fail closed: leave no half-load behind. Best effort; the original error is what matters."""
    try:
        execute(data, [f"DELETE FROM public.{t}" for t in (*TABLES, LOG_TABLE)], sleep)  # noqa: S608 - table names are this file's constants
    except Exception:  # noqa: BLE001
        return


def verify_after(data: Any, shape: ShapeRun, counts: dict[str, int], sleep: Any) -> None:
    rows = select(
        data,
        'SELECT "position", "month", s, n_obs FROM public.ecp_shape_params'
        ' ORDER BY "position", "month"',
    )
    if len(rows) != SHAPE_ROWS:
        raise PostLoadMismatch(f"the warehouse holds {len(rows)} shape rows, not 48")
    try:
        back = [(str(p), int(m), Decimal(str(s)), int(n)) for p, m, s, n in rows]
    except (ValueError, InvalidOperation):
        raise PostLoadMismatch("a shape row read back is not a number") from None
    if text_sha256(canonical_text(back)) != shape.params_sha256:
        raise PostLoadMismatch("the shape rows read back do not rebuild the recorded hash")
    for table in TABLES[1:]:
        (cell,) = select(data, f"SELECT COUNT(*) FROM public.{table}")[0]  # noqa: S608 - table names are this file's constants
        if int(cell) != counts[table]:
            raise PostLoadMismatch(f"{table} holds {cell} rows, not the manifest's {counts[table]}")


def data_api_client(sts: Any, account: str, region: str) -> Any:
    """The Data API as `ecp-redshift-data-api`, assumed from the operator's own session."""
    import boto3

    creds = sts.assume_role(
        RoleArn=f"arn:aws:iam::{account}:role/{CALLER_ROLE}",
        RoleSessionName="ecp-redshift-load",
        DurationSeconds=3600,
    )["Credentials"]
    return boto3.client(
        "redshift-data",
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


class NoDataApi:
    """A dry run never reaches the Data API; this makes sure of it."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError("a dry run must not call the Data API")


def client(name: str) -> Any:
    import boto3

    return boto3.client(name, region_name=REGION)


def expected_account(flag: str | None) -> str:
    """The member account this stack lives in: --expected-account, else the git-ignored tfvars."""
    value = flag
    if value is None and TFVARS.exists():
        found = re.search(r'(?m)^\s*expected_account_id\s*=\s*"([^"]*)"', TFVARS.read_text())
        value = found.group(1) if found else None
    if value is None:
        raise LoadRefused("no expected account: pass --expected-account or fill terraform.tfvars")
    if not re.fullmatch(r"\d{12}", value):
        raise LoadRefused("the expected account is not a 12-digit account ID")
    return value


def check_caller(sts: Any, org: Any, expected: str) -> str:
    """Print who is calling (masked), then refuse root, the wrong account and the organization's
    management account, before any S3, assume-role or Data API call. Returns the account."""
    who = sts.get_caller_identity()
    arn, account = str(who["Arn"]), str(who["Account"])
    root = bool(re.search(r":root$", arn))
    role = re.search(r"assumed-role/([^/]+)/", arn)
    name = role.group(1) if role else mask(arn.rsplit(":", 1)[-1])
    try:
        master = org.describe_organization()["Organization"]["MasterAccountId"]
    except Exception:  # noqa: BLE001 - not knowing the management account is a refusal
        master = None
    kind = (
        "unknown account type"
        if master is None
        else ("management account" if master == account else "member")
    )
    print(f"identity: {name}, {kind}, {'root' if root else 'not root'}")  # noqa: T201
    if root:
        raise LoadRefused("the caller is the root user")
    if master is None:
        raise LoadRefused("cannot tell whether this is the management account")
    if master == account:
        raise LoadRefused("the caller is in the organization's management account")
    if account != expected:
        raise LoadRefused("the caller is not in the expected account")
    return account


def main(argv: list[str] | None = None) -> int:
    from botocore.exceptions import BotoCoreError, ClientError

    parser = argparse.ArgumentParser(prog="redshift_load.py")
    parser.add_argument("--run-id", help="the Glue run to load; default: the newest completed one")
    parser.add_argument("--dry-run", action="store_true", help="verify everything, send nothing")
    parser.add_argument(
        "--expected-account", help="default: expected_account_id in terraform.tfvars"
    )
    args = parser.parse_args(argv)
    try:
        expected = expected_account(args.expected_account)
        sts = client("sts")
        account = check_caller(sts, client("organizations"), expected)
        s3 = client("s3")
        data = NoDataApi() if args.dry_run else data_api_client(sts, account, REGION)
        summary = run_load(
            s3,
            data,
            f"ecp-glue-{account}-{REGION}",
            f"ecp-data-{account}-{REGION}",
            f"arn:aws:iam::{account}:role/{COPY_ROLE}",
            run_id=args.run_id,
            dry_run=args.dry_run,
        )
    except (LoadRefused, PostLoadMismatch, LoadFailed) as exc:
        print(f"REFUSED: {mask(str(exc))}")  # noqa: T201
        return 1
    except (ClientError, BotoCoreError) as exc:
        print(f"AWS ERROR: {mask(str(exc))}")  # noqa: T201
        return 1
    print(summary.text())  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
