"""Describe one real capture sample for G-FORMAT review; register nothing.

Usage (the sample is read only, and nothing is written unless ``--out`` is set):

    py -3.13 scripts/inspect_capture_sample.py path/to/capture.csv \
        --tool-name PresentMon --tool-version 2.3.1 --origin real \
        --option "--process_name=ForzaHorizon6.exe" --out sample.json

``--origin`` is required: ``real`` for bytes an actual tool wrote, ``simulated``
for hand-authored stand-ins.  Only a ``real`` sample can ever be cited as
G-FORMAT evidence; a simulated one exercises this reader and nothing more.

The output records the file hash, the header as stated, units, process ids,
stream values, frame-generation values, the time column and its monotonicity.
A format may only be registered after a human reviews this report; this script
does not register anything and does not claim tool support.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from game_app.adapters.presentmon import inspect_sample  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", help="the original CSV to describe")
    parser.add_argument("--tool-name", required=True)
    parser.add_argument("--tool-version", required=True)
    parser.add_argument(
        "--origin", required=True, choices=["real", "simulated", "unknown"],
        help="where the bytes came from; only 'real' is citable G-FORMAT evidence",
    )
    parser.add_argument(
        "--option", action="append", default=[],
        help="one exact capture option; repeat for each",
    )
    parser.add_argument("--out", default=None, help="write the report here")
    args = parser.parse_args(argv)

    path = Path(args.sample)
    if not path.is_file():
        sys.stderr.write("the sample path is not an existing file\n")
        return 2
    data = path.read_bytes()
    inspection = inspect_sample(
        data, source_name=path.name, declared_origin=args.origin,
    )
    report = {
        "kind": "capture_sample_inspection",
        "tool": {"name": args.tool_name, "version": args.tool_version},
        "options": list(args.option),
        "declared_origin": inspection.declared_origin,
        "source_name": inspection.source_name,
        "byte_length": inspection.byte_length,
        "sha256": inspection.sha256,
        "header": list(inspection.header),
        "columns": [
            {
                "header_token": column.header_token,
                "name": column.name,
                "unit": column.unit,
                "role": column.role,
            }
            for column in inspection.columns
        ],
        "row_count": inspection.row_count,
        "malformed_rows": inspection.malformed_rows,
        "process_ids": list(inspection.process_ids),
        "stream_values": list(inspection.stream_values),
        "frame_generation_values": list(inspection.frame_generation_values),
        "time_column": inspection.time_column,
        "time_monotonic": inspection.time_monotonic,
        "interval_column": inspection.interval_column,
        "blockers": list(inspection.blockers),
        "registration_ready": inspection.registration_ready,
        "citable_for_g_format": inspection.citable_for_g_format,
        "tool_support_claimed": False,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0 if inspection.registration_ready else 3


if __name__ == "__main__":
    raise SystemExit(main())
