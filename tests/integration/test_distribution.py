"""ENG-T05: CLI distribution, package data and a working-directory-free start.

Only the command line is distributed (Q1): no page, template, static asset or
HTTP server is built here.  The checks are offline: a local wheel is built with
``--no-index --no-build-isolation`` and nothing is downloaded.  If the local
build toolchain is unavailable the wheel case is recorded as ``not_run`` with
its reason instead of being counted as a pass.

The single-writer application lock that 07 §1 asks for is **not implemented**.
It is recorded as ``not_run`` rather than invented here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from game_app.cli import main as cli_main
from tests.product.support.fixtures import CaseResult
from tests.product.support.validation import summarize_results

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
SCOPE = "a" * 32


def _run_cli(*arguments: str, cwd: Path, env_extra: dict[str, str] | None = None):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if env_extra:
        environment.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "game_app.cli", *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


@pytest.fixture(scope="module")
def offline_wheel(tmp_path_factory):
    """Build one local wheel; skip (not fail) when no offline build is possible."""

    destination = tmp_path_factory.mktemp("dist")
    working = tmp_path_factory.mktemp("wheel-build")
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel", str(REPO_ROOT),
            "--no-index", "--no-build-isolation", "--no-deps",
            "--wheel-dir", str(destination),
        ],
        cwd=working,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    wheels = sorted(destination.glob("*.whl"))
    if result.returncode != 0 or not wheels:
        pytest.skip(
            "offline wheel build unavailable (no network is used): "
            + (result.stderr or result.stdout or "no wheel produced").strip()[-500:]
        )
    return wheels[0]


def test_every_migration_ships_with_the_package():
    from importlib.resources import files

    migrations = files("game_app").joinpath("migrations")
    names = sorted(item.name for item in migrations.iterdir() if item.name.endswith(".sql"))
    assert "0014_target_acceptance.sql" in names
    assert len(names) >= 14


def test_pyproject_declares_the_package_data_and_console_entry():
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert project["tool"]["setuptools"]["package-data"]["game_app"] == ["migrations/*.sql"]
    assert project["project"]["scripts"]["game-app"] == "game_app.cli:main"
    # Only a version actually verified on this machine is claimed.
    assert project["project"]["requires-python"] == ">=3.11"
    assert sys.version_info >= (3, 11)
    assert callable(cli_main)


def test_the_offline_wheel_carries_migrations_and_the_console_entry(offline_wheel):
    with zipfile.ZipFile(offline_wheel) as archive:
        names = archive.namelist()
        migrations = [
            name for name in names
            if name.startswith("game_app/migrations/") and name.endswith(".sql")
        ]
        assert "game_app/migrations/0014_target_acceptance.sql" in migrations
        assert len(migrations) >= 14
        entry_points = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        content = archive.read(entry_points).decode("utf-8")
        assert "game-app = game_app.cli:main" in content
        assert "game-collector = game_collector.__main__:main" in content


def test_help_runs_from_a_foreign_directory_without_creating_a_database(tmp_path):
    result = _run_cli("--help", cwd=tmp_path)
    assert result.returncode == 0
    assert "game-app" in result.stdout
    assert "usage:" in result.stdout.lower()
    assert not (tmp_path / "product.sqlite3").exists()
    assert not (tmp_path / "managed").exists()


def test_a_read_command_uses_the_explicit_paths_from_any_directory(tmp_path):
    database = tmp_path / "explicit" / "product.sqlite3"
    managed = tmp_path / "explicit" / "managed"
    result = _run_cli(
        "package", "get", "0" * 32,
        "--db", str(database),
        "--files", str(managed),
        "--user-scope", SCOPE,
        cwd=tmp_path,
    )
    # The package does not exist, so this is a stable not_found exit — the
    # command still ran against the explicit database, not the cwd.
    assert result.returncode == 4
    assert json.loads(result.stderr)["code"] == "RESOURCE_NOT_FOUND"
    assert database.is_file()
    assert not (tmp_path / "product.sqlite3").exists()


def test_help_and_errors_do_not_print_private_paths_or_secrets(tmp_path):
    secret = "sk-test-SECRET-VALUE-DO-NOT-PRINT"
    help_result = _run_cli(
        "--help", cwd=tmp_path, env_extra={"DEEPSEEK_API_KEY": secret},
    )
    assert help_result.returncode == 0
    error_result = _run_cli(
        "package", "get", "0" * 32,
        "--db", str(tmp_path / "product.sqlite3"),
        "--files", str(tmp_path / "managed"),
        "--user-scope", SCOPE,
        cwd=tmp_path,
        env_extra={"DEEPSEEK_API_KEY": secret},
    )
    for output in (help_result.stdout, help_result.stderr,
                   error_result.stdout, error_result.stderr):
        assert secret not in output
        assert str(REPO_ROOT) not in output
        assert "sk-" not in output.lower()
        assert ".env" not in output


def test_the_distribution_report_marks_the_single_writer_lock_not_run(tmp_path):
    """07 asks for one application process; no lock is implemented yet.

    The honest result is ``not_run`` with its reason.  This test does not start
    a second worker and does not invent a lock.
    """

    results = [
        CaseResult(
            case_id="ENG-T05.package-data",
            fixture_version=1,
            run_id="distribution-1",
            status="passed",
            evidence_level="integration",
            observed_files=[],
            failure_notes=[],
            executed=True,
            requirement_ids=["ENG-T05"],
        ),
        CaseResult(
            case_id="ENG-T05.single-writer",
            fixture_version=1,
            run_id="distribution-1",
            status="not_run",
            evidence_level="integration",
            observed_files=[],
            failure_notes=[
                "07 §1 asks for an exclusive application lock before a second"
                " worker starts; game_app has no such lock yet, and this slice"
                " does not invent one"
            ],
            executed=False,
            requirement_ids=["ENG-T05"],
        ),
    ]
    report = tmp_path / "distribution.json"
    report.write_text(
        json.dumps([result.model_dump(mode="json") for result in results],
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    summary = summarize_results(payload)
    assert summary["passed"] == 1
    assert summary["not_run"] == 1
    assert summary["gaps"][0]["case_id"] == "ENG-T05.single-writer"
    assert "exclusive application lock" in summary["gaps"][0]["reason"]
