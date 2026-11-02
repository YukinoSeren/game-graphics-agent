"""Read-only Windows probe adapter.

Only packaged, constant queries are executed.  Callers cannot pass a command,
an expression or a path: an unregistered field path is refused before anything
runs.  The adapter is deliberately untested against real hardware in this
slice (G-WIN evidence is still pending), so it never runs during the test suite.
"""

from __future__ import annotations

import re
import subprocess
from typing import Sequence

from game_app.device import ProbeReading

_GPU_INDEX = re.compile(r"^gpus\.(\d+)\.(raw_name|vram_bytes|driver_version)$")
_MAX_GPU_INDEX = 15

_QUERIES: dict[str, str] = {
    "cpu": "Get-CimInstance -ClassName Win32_Processor | Select-Object -First 1 -ExpandProperty Name",
    "memory_bytes": "Get-CimInstance -ClassName Win32_ComputerSystem | Select-Object -ExpandProperty TotalPhysicalMemory",
    "os_version": "[System.Environment]::OSVersion.Version.ToString()",
    "device_form": "Get-CimInstance -ClassName Win32_ComputerSystem | Select-Object -ExpandProperty PCSystemType",
    "display.desktop_resolution.width": "Get-CimInstance -ClassName Win32_VideoController | Where-Object { $_.CurrentHorizontalResolution } | Select-Object -First 1 -ExpandProperty CurrentHorizontalResolution",
    "display.desktop_resolution.height": "Get-CimInstance -ClassName Win32_VideoController | Where-Object { $_.CurrentVerticalResolution } | Select-Object -First 1 -ExpandProperty CurrentVerticalResolution",
    "display.refresh_hz": "Get-CimInstance -ClassName Win32_VideoController | Where-Object { $_.CurrentRefreshRate } | Select-Object -First 1 -ExpandProperty CurrentRefreshRate",
}

_GPU_QUERIES = {
    "raw_name": "Name",
    "vram_bytes": "AdapterRAM",
    "driver_version": "DriverVersion",
}


def query_for(field_path: str) -> str:
    """Return the fixed query for a whitelisted path; refuse everything else."""
    if field_path in _QUERIES:
        return _QUERIES[field_path]
    match = _GPU_INDEX.match(field_path)
    if match is None:
        raise ValueError(f"unregistered probe path: {field_path}")
    index, attribute = int(match.group(1)), match.group(2)
    if index > _MAX_GPU_INDEX:
        raise ValueError("gpu index out of range")
    # The index is an integer parsed from the path, never caller text.
    return (
        f"$g = @(Get-CimInstance -ClassName Win32_VideoController); "
        f"if ($g.Length -gt {index}) {{ $g[{index}].{_GPU_QUERIES[attribute]} }}"
    )


class WindowsProbeAdapter:
    def probe(self, field_paths: Sequence[str], *, timeout: float) -> list[ProbeReading]:
        queries = [(path, query_for(path)) for path in field_paths]
        readings: list[ProbeReading] = []
        for path, query in queries:
            try:
                completed = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        query,
                    ],
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                readings.append(ProbeReading(
                    field_path=path, status="failed", raw=b"", error_code="PROBE_TIMEOUT",
                ))
                continue
            except OSError as error:
                readings.append(ProbeReading(
                    field_path=path, status="failed", raw=b"",
                    error_code="PROBE_UNAVAILABLE",
                ))
                continue
            raw = completed.stdout
            if completed.returncode != 0 or not raw.strip():
                readings.append(ProbeReading(
                    field_path=path, status="failed", raw=raw,
                    error_code="PROBE_FAILED",
                ))
                continue
            readings.append(ProbeReading(
                field_path=path, status="succeeded", raw=raw,
                text=raw.decode("utf-8", errors="replace").strip(),
            ))
        return readings


__all__ = ["WindowsProbeAdapter", "query_for"]
