import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline.lock import PipelineLocked, single_writer
from energy_curves.pipeline.runner import FetchRequest, run_ingest

# A separate interpreter holds the lock, like a second CLI run would. (Forking the test process
# is unsafe once Polars' thread pool exists: the child can deadlock on an inherited lock.)
HOLDER = """
import sys
from pathlib import Path
from energy_curves.pipeline.lock import single_writer
with single_writer(Path(sys.argv[1])):
    print("acquired", flush=True)
    sys.stdin.readline()
"""
JAN = [FetchRequest(("RWTC",), date(2024, 1, 1), date(2024, 1, 31))]


@pytest.fixture
def holder(tmp_path: Path):  # type: ignore[no-untyped-def]
    proc = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-c", HOLDER, str(tmp_path / ".pipeline.lock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "acquired"
    yield proc
    proc.kill()
    proc.wait(10)


def test_overlapping_run_is_refused(tmp_path: Path, holder) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PipelineLocked):
        run_ingest(tmp_path, SyntheticSource(), JAN, source_name="synthetic")
    assert not (tmp_path / "published").exists()  # the refused run wrote nothing
    assert not (tmp_path / "runs").exists()


def test_lock_released_when_holder_is_killed(tmp_path: Path, holder) -> None:  # type: ignore[no-untyped-def]
    holder.kill()  # SIGKILL: no cleanup code runs in the holder
    holder.wait(10)
    with single_writer(tmp_path / ".pipeline.lock"):
        pass  # the kernel released the lock; nothing stale remains


def test_run_proceeds_after_holder_finishes(tmp_path: Path, holder) -> None:  # type: ignore[no-untyped-def]
    holder.communicate("\n", timeout=10)
    assert run_ingest(tmp_path, SyntheticSource(), JAN, source_name="synthetic").status == (
        "published"
    )


def test_lock_released_after_exception(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), single_writer(tmp_path / ".pipeline.lock"):
        raise RuntimeError("boom")
    with single_writer(tmp_path / ".pipeline.lock"):
        pass
