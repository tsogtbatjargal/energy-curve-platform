"""M5b (ADR-0023): the zip that carries the job's modules to Glue is built deterministically.

`--extra-py-files` takes individual files, never a directory, so the package modules the job
imports travel as one zip. Its hash is pinned by the plan check, so what Glue runs is exactly what
the repository holds.
"""

import ast
import hashlib
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import build_glue_bundle as bundle
import pytest

ROOT = Path(__file__).parents[1]
MEMBERS = [
    "energy_curves/__init__.py",
    "energy_curves/catalog.py",
    "energy_curves/curves/__init__.py",
    "energy_curves/curves/shape_core.py",
    "energy_curves/synthetic_prices.py",
]
STDLIB = set(sys.stdlib_module_names) | {"__future__"}


def test_the_bundle_holds_exactly_the_import_closure_of_the_job() -> None:
    assert bundle.MEMBERS == MEMBERS
    with zipfile.ZipFile(io.BytesIO(bundle.build_bytes())) as z:
        assert z.namelist() == MEMBERS


def test_each_member_is_the_repository_file() -> None:
    with zipfile.ZipFile(io.BytesIO(bundle.build_bytes())) as z:
        for name in MEMBERS:
            assert z.read(name) == (ROOT / "src" / name).read_bytes()


def test_two_builds_are_byte_identical(tmp_path: Path) -> None:
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    digest_a, digest_b = bundle.write(a), bundle.write(b)
    assert a.read_bytes() == b.read_bytes()
    assert digest_a == digest_b == hashlib.sha256(a.read_bytes()).hexdigest()


def test_entries_have_fixed_timestamps_and_permissions() -> None:
    with zipfile.ZipFile(io.BytesIO(bundle.build_bytes())) as z:
        for info in z.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.external_attr >> 16 == 0o100644
            assert info.compress_type == zipfile.ZIP_STORED  # no zlib version in the bytes
            assert info.create_system == 3


def test_the_build_does_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    want = bundle.build_bytes()
    monkeypatch.chdir(tmp_path)
    assert bundle.build_bytes() == want


def test_every_member_imports_only_the_standard_library_and_the_bundle() -> None:
    own = {"energy_curves"}
    for name in MEMBERS:
        tree = ast.parse((ROOT / "src" / name).read_text())
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            for m in modules:
                assert m.split(".")[0] in STDLIB | own, (name, m)


def test_the_bundle_imports_in_a_bare_interpreter(tmp_path: Path) -> None:
    """With no site-packages at all (as on a Glue executor before the job's own files)."""
    path = tmp_path / "energy_curves_m5.zip"
    bundle.write(path)
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from energy_curves.synthetic_prices import history;"
        "from energy_curves.curves.shape_core import quantize_s;"
        "import datetime as d;"
        "print(len(list(history(('RWTC',), d.date(2024,1,1), d.date(2024,1,7)))))"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-S", "-c", code, str(path)], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "5"
