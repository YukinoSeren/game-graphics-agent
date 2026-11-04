"""Run the packaged read-only Windows probe once, for G-WIN evidence.

Every query comes from ``game_app.adapters.windows_probe.query_for``: a caller
cannot add a command, an expression or a path.  This script only runs when the
operator has authorized a real local probe (``--i-authorize-read-only-probe``),
and it never starts a game, a capture tool or a model call.

    py -3.13 scripts/probe_device_fields.py --i-authorize-read-only-probe \
        --out gwin-probe.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from game_app.adapters.windows_probe import WindowsProbeAdapter  # noqa: E402

BASE_FIELDS = (
    "cpu",
    "memory_bytes",
    "os_version",
    "device_form",
    "display.desktop_resolution.width",
    "display.desktop_resolution.height",
    "display.refresh_hz",
)
GPU_ATTRIBUTES = ("raw_name", "vram_bytes", "driver_version")
MAX_GPU = 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--i-authorize-read-only-probe", action="store_true",
        help="required: confirms the operator authorized a real local probe",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    if not args.i_authorize_read_only_probe:
        sys.stderr.write(
            "refused: a real probe needs --i-authorize-read-only-probe\n"
        )
        return 2

    fields = list(BASE_FIELDS)
    for index in range(MAX_GPU):
        for attribute in GPU_ATTRIBUTES:
            fields.append(f"gpus.{index}.{attribute}")

    readings = WindowsProbeAdapter().probe(fields, timeout=args.timeout)
    report = {
        "kind": "gwin_read_only_probe",
        "authorized": True,
        "fields": [
            {
                "field_path": reading.field_path,
                "status": reading.status,
                "text": reading.text,
                "error_code": reading.error_code,
            }
            for reading in readings
        ],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
