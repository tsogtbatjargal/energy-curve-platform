"""Build the zip that carries the Glue job's package modules (M5b, ADR-0023).

    python scripts/build_glue_bundle.py [output.zip]

The default output is infra/glue/build/energy_curves_m5.zip (git-ignored).

Glue's `--extra-py-files` takes individual files, never a directory, so the modules the job
imports (`shape_job.py` imports only these) travel as one zip. The bytes depend only on the file
contents: sorted members, fixed timestamps and permissions, stored without compression (so no zlib
version enters the bytes). The plan check recomputes this and compares it with the planned S3
object, so what Glue runs is exactly what the repository holds.
"""

from __future__ import annotations

import hashlib
import io
import sys
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1] / "infra" / "glue" / "build" / "energy_curves_m5.zip"
)
MEMBERS = [
    "energy_curves/__init__.py",
    "energy_curves/catalog.py",
    "energy_curves/curves/__init__.py",
    "energy_curves/curves/shape_core.py",
    "energy_curves/synthetic_prices.py",
]
TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def build_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as z:
        for name in sorted(MEMBERS):
            info = zipfile.ZipInfo(name, TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3  # Unix
            info.external_attr = 0o100644 << 16
            z.writestr(info, (SRC / name).read_bytes())
    return buffer.getvalue()


def write(path: Path) -> str:
    """Write the bundle and return its SHA-256."""
    data = build_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    output = Path(args[0]) if args else DEFAULT_OUTPUT
    print(f"{write(output)}  {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
