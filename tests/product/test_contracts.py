"""VAL-T02: the deterministic case matrix and its honest gaps.

Each module V case is registered here.  Cases whose scenario is implemented
run the module's own acceptance test against a fresh temporary store; cases
that need a real environment (Windows/PresentMon, a real game, a browser) are
``blocked`` with the exact missing evidence.  Nothing is marked passed unless
it actually executed, and an all-skipped run cannot be reported as a pass.
"""

from __future__ import annotations

import hashlib
import importlib
import json

import pytest
from pydantic import ValidationError

from game_app.application import Application
from game_app.contracts.performance import (
    CreateTestProtocolRequest,
    MetricCatalogQuery,
)
from game_app.contracts.knowledge import GameCatalogQuery
from game_app.contracts.primitives import BindingRef
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileService
from game_app.importer import (
    RESEARCH_SOURCE_SECTIONS_FORMAT_ID,
    ResearchImporter,
    map_research_document,
)
from game_app.knowledge import IdentityService
from game_app.repository import ProductRepository, canonical_json
from tests.product.support.configuration import context_key, setup
from tests.product.support.fixtures import CaseResult
from tests.product.support.performance import (
    analysis_input,
    artifact_from_csv,
    craft_artifact,
    csv_bytes,
    publish_log,
)
from tests.product.support.validation import (
    PRODUCT_FIXTURES,
    InsufficientEvidence,
    ScenarioRunner,
    conclude_results,
    load_results,
    summarize_results,
)
from tests.product.test_sessions import FakeClock

# All 63 module V cases from 08 §2, plus the three B-* base cases.  A case may
# be registered under a suffixed id (``VAL-V02-full`` covers ``VAL-V02``); the
# coverage check below normalises that, so a family cannot hide behind a name.
REQUIRED_CASES = {
    "FBK-V01", "FBK-V02", "FBK-V03", "FBK-V04", "FBK-V05", "FBK-V06",
    "REC-V01", "REC-V02", "REC-V03", "REC-V04", "REC-V05",
    "PERF-V01", "PERF-V02", "PERF-V03", "PERF-V04", "PERF-V05",
    "PERF-V06", "PERF-V07", "PERF-V08", "PERF-V09",
    "DEV-V01", "DEV-V02", "DEV-V03", "DEV-V04",
    "DEV-V05", "DEV-V06", "DEV-V07", "DEV-V08",
    "IMP-V01", "IMP-V02", "IMP-V03", "IMP-V04", "IMP-V05",
    "KNW-V01", "KNW-V02", "KNW-V03", "KNW-V04", "KNW-V05", "KNW-V06",
    "ENG-V01", "ENG-V02", "ENG-V03", "ENG-V04", "ENG-V05", "ENG-V06",
    "ENG-V07",
    "UX-V01", "UX-V02", "UX-V03", "UX-V04", "UX-V05", "UX-V06",
    "UX-V07", "UX-V08", "UX-V09",
    "VAL-V01", "VAL-V02", "VAL-V03", "VAL-V04", "VAL-V05",
    "VAL-V06", "VAL-V07", "VAL-V08",
    "B-DAT-14", "B-STA-13", "B-ERR-12",
}
F1_CASES = {"IMP-V04-research-retained", "IMP-V04-research-mapping"}
# Cases this batch added for the CLI entry surface (Q1).
CLI_CASES = {"UX-V01-cli-entry"}

_CASE_FAMILY = __import__("re").compile(r"^((?:FBK|REC|PERF|DEV|IMP|KNW|ENG|UX|VAL)-V\d{2})")


def _family(case_id: str) -> str:
    """``VAL-V02-full`` -> ``VAL-V02``; ``B-DAT-14`` -> itself."""

    match = _CASE_FAMILY.match(case_id)
    return match.group(1) if match else case_id

# case_id -> (module, test function, requirement ids, purpose, assertions)
MODULE_CASES = {
    "FBK-V01": (
        "tests.product.test_feedback",
        "test_a_successful_parse_links_a_new_feedback_and_keeps_the_original",
        ["C11", "FBK-V01"],
        "an unparsed original survives and a successful parse links a new record",
        ["the original words are kept", "the interpretation links to it", "no diagnosis is fabricated"],
    ),
    "FBK-V02": (
        "tests.product.test_feedback",
        "test_a_withdrawal_appends_a_version_and_keeps_the_old_one",
        ["D04", "FBK-V02"],
        "a preference withdrawal adds a version and never edits the old one",
        ["the old preference is unchanged", "the withdrawal is a new version"],
    ),
    "FBK-V03": (
        "tests.product.test_next_actions",
        "test_a_legal_adjust_writes_a_change_set_without_moving_the_pointer",
        ["D01", "FBK-V03"],
        "a legal adjust writes a ChangeSet and leaves the current pointer alone",
        ["a ChangeSet exists", "the applied pointer did not move"],
    ),
    "FBK-V04": (
        "tests.product.test_outcomes",
        "test_a_tested_config_without_this_change_set_is_refused",
        ["D01", "FBK-V04"],
        "an outcome must name the experiment it tested",
        ["mismatched configuration is refused", "no outcome is written"],
    ),
    "FBK-V05": (
        "tests.product.test_outcomes",
        "test_average_up_but_tail_down_is_inconclusive",
        ["D05", "FBK-V05"],
        "a higher average with a worse tail is inconclusive, not improved",
        ["the result is inconclusive", "acceptance is separate from effect"],
    ),
    "FBK-V06": (
        "tests.product.test_next_actions",
        "test_a_refused_direction_is_not_repeated_without_new_evidence",
        ["FBK-V06"],
        "a failed direction is not repeated without new evidence",
        ["the repeat is blocked", "no duplicate attempt is created"],
    ),
    "REC-V01": (
        "tests.product.test_recommendation_gate",
        "test_an_own_unpublished_package_is_never_an_input",
        ["C01", "C02", "REC-V01"],
        "an unpublished package is never a recommendation input",
        ["the reference is refused", "no recommendation version is written"],
    ),
    "REC-V02": (
        "tests.product.test_recommendation_gate",
        "test_a_same_named_laptop_with_another_gpu_is_not_a_direct_match",
        ["C05", "REC-V02"],
        "a same-named laptop with another GPU is not a direct match",
        ["the match is general or conditional", "FPS is not migrated"],
    ),
    "REC-V03": (
        "tests.product.test_recommendation_actions",
        "test_two_mutually_exclusive_branches_are_not_both_set",
        ["C04", "REC-V03"],
        "mutually exclusive branches never both become fixed values",
        ["the conflicting branch is conditional", "neither is claimed as fixed"],
    ),
    "REC-V04": (
        "tests.product.test_recommendation_actions",
        "test_an_interval_target_stays_an_interval",
        ["C06", "REC-V04"],
        "an interval target stays an interval and is never averaged",
        ["no midpoint is computed", "the target keeps its bounds"],
    ),
    "REC-V05": (
        "tests.product.test_recommendation_review",
        "test_a_new_version_supersedes_the_old_one_and_leaves_it_unchanged",
        ["D02", "REC-V05"],
        "a new version supersedes without editing the old one",
        ["the old version is unchanged", "the new version is a new id"],
    ),
    "PERF-V03": (
        "tests.product.test_metrics",
        "test_spike_sequence_is_kept_and_adaptive_threshold_is_per_run",
        ["D05", "PERF-V03"],
        "the common threshold keeps spikes on both sides",
        ["the spike is kept", "the adaptive threshold stays per run"],
    ),
    "PERF-V04": (
        "tests.product.test_comparisons",
        "test_cluster_rate_is_per_minute_and_zero_duration_is_unknown",
        ["D05", "PERF-V04"],
        "cluster rate is per minute and a zero denominator is unknown",
        ["two clusters over one minute is 2/min", "zero duration is unknown, not 0"],
    ),
    "PERF-V05": (
        "tests.product.test_comparisons",
        "test_unknown_configuration_is_never_controlled",
        ["D03", "PERF-V05"],
        "an unknown configuration is never reported as controlled",
        ["the configuration stays unknown", "the comparison is not controlled"],
    ),
    "PERF-V07": (
        "tests.product.test_comparisons",
        "test_derived_threshold_is_frozen_and_replays_without_recompute",
        ["D02", "D05", "PERF-V07"],
        "a derived threshold is frozen and a replay does not recompute",
        ["the protocol id is stable", "frozen_at does not move"],
    ),
    "PERF-V08": (
        "tests.product.test_comparisons",
        "test_post_hoc_non_exploratory_is_rejected_and_exploratory_is_allowed",
        ["D03", "PERF-V08"],
        "after-the-fact comparisons must be exploratory",
        ["the non-exploratory path is refused", "the exploratory path is allowed"],
    ),
    "DEV-V01": (
        "tests.product.test_information_answers",
        "test_partial_answer_keeps_unanswered_fields_and_the_task_waiting",
        ["C11", "DEV-V01"],
        "a partial answer keeps the unanswered fields and the task waiting",
        ["answers are scoped to the submitted fields", "the task stays waiting"],
    ),
    "DEV-V02": (
        "tests.product.test_device",
        "test_identical_gpu_names_stay_separate_and_none_is_selected",
        ["C01", "C05", "DEV-V02"],
        "identical GPU names stay separate and none is silently selected",
        ["two adapters are kept", "no first item is chosen"],
    ),
    "DEV-V03": (
        "tests.product.test_adoption",
        "test_a_preset_only_adoption_is_legal",
        ["C07", "D03", "DEV-V03"],
        "a preset-only baseline is legal and stays unverified",
        ["the adoption is recorded", "the value is not expanded into a recipe"],
    ),
    "DEV-V04": (
        "tests.product.test_adoption",
        "test_change_set_merges_from_the_frozen_base_and_keeps_unknown_entries",
        ["D01", "DEV-V04"],
        "a ChangeSet merges from the frozen base and keeps unknown entries",
        ["unchanged entries survive", "unknown entries are not dropped"],
    ),
    "IMP-V01": (
        "tests.product.test_import_freeze",
        "test_changed_file_during_the_copy_window_is_rejected",
        ["IMP-V01"],
        "a group that changes during the copy is refused",
        ["IMPORT_SNAPSHOT_INCONSISTENT", "the source directory is unchanged"],
    ),
    "IMP-V02": (
        "tests.product.test_scoped_import",
        "test_same_local_id_in_two_runs_and_sources_stays_two_records",
        ["IMP-V02"],
        "the same local id in another run/source is a separate record",
        ["neither record overwrites the other", "both keys are four-part keys"],
    ),
    "IMP-V03": (
        "tests.product.test_import_publish",
        "test_rename_then_crash_leaves_an_unconsumable_orphan_and_retry_reuses_it",
        ["D02", "IMP-V03"],
        "an orphan after rename is not consumed and the same key reuses it",
        ["one snapshot identity", "no partial record is readable"],
    ),
    "IMP-V05": (
        "tests.product.test_scoped_import",
        "test_quarantined_stays_quarantined_and_accepted_is_not_upgraded",
        ["IMP-V05"],
        "the legacy status and envelope are preserved, never upgraded",
        ["quarantined stays quarantined", "the envelope keeps every field"],
    ),
    "KNW-V01": (
        "tests.product.test_collection_tasks",
        "test_collection_starts_without_a_target_fps_preference_or_device",
        ["C01", "C02", "KNW-V01"],
        "collection starts from the game context alone",
        ["no FPS or device input is required", "the provider stub is counted"],
    ),
    "KNW-V02": (
        "tests.product.test_collection_tasks",
        "test_two_rounds_without_gain_stop_with_low_information_gain",
        ["C03", "KNW-V02"],
        "two gain-less rounds stop with an honest partial result",
        ["the stop reason is low information gain", "the collected members are kept"],
    ),
    "KNW-V03": (
        "tests.product.test_packages",
        "test_an_isolated_record_is_diagnostic_only",
        ["IMP-V02", "KNW-V03"],
        "an isolated record never becomes a package member",
        ["the package excludes it", "the diagnostic original stays readable"],
    ),
    "KNW-V04": (
        "tests.product.test_impacts",
        "test_the_image_adapter_stays_disabled_without_evidence",
        ["C08", "KNW-V04"],
        "the image adapter stays disabled without independent evidence",
        ["no image capability is claimed", "the gap stays open"],
    ),
    "KNW-V05": (
        "tests.product.test_impacts",
        "test_the_notice_is_consumable_by_the_03_consumer",
        ["C09", "KNW-V05"],
        "a source update becomes one review event and never edits the recommendation",
        ["one needs_review event", "the recommendation content is unchanged"],
    ),
    "ENG-V01": (
        "tests.product.test_faults",
        "test_same_key_concurrent_registration_creates_one_task",
        ["D02", "ENG-V01"],
        "concurrent same-key registration creates exactly one task",
        ["one task row", "one idempotency row"],
    ),
    "ENG-V02": (
        "tests.product.test_information_answers",
        "test_answering_first_then_cancelling_stops_the_queued_task",
        ["D04", "ENG-V02"],
        "a late cancel does not revive or deadlock the task",
        ["the task stays cancelled", "no second execution starts"],
    ),
    "ENG-V03": (
        "tests.product.test_adoption",
        "test_stale_expected_current_conflicts_with_zero_partial_write",
        ["D01", "ENG-V03"],
        "a stale expected revision conflicts with zero partial writes",
        ["IDEMPOTENCY/CONFLICT outcome", "the current pointer is unchanged"],
    ),
    "ENG-V04": (
        "tests.product.test_files",
        "test_crash_after_rename_leaves_unconsumable_orphan_and_retry_reuses_it",
        ["A05", "ENG-V04"],
        "a file renamed but not committed is not consumable",
        ["the orphan is unreadable", "the same key retries the frozen file"],
    ),
    "ENG-V05": (
        "tests.product.test_faults",
        "test_no_checkpoint_recovery_creates_one_child_and_does_not_resend",
        ["A06", "ENG-V05"],
        "a recovery with no checkpoint never resends an unknown call",
        ["the unknown send count is unchanged", "the parent task stays interrupted"],
    ),
    "ENG-V06": (
        "tests.product.test_files",
        "test_windows_drive_prefixed_names_cannot_escape_the_managed_root",
        ["C09", "ENG-V06"],
        "a managed file name cannot escape the managed root",
        ["the path is refused", "nothing is written outside the root"],
    ),
    "UX-V01": (
        "tests.product.test_collection_tasks",
        "test_collection_starts_without_a_target_fps_preference_or_device",
        ["C02", "C03", "UX-V01"],
        "the entry contract needs only a game; no FPS/image field is required",
        ["a task is accepted from the game context", "partial results are recoverable"],
    ),
    "UX-V03": (
        "tests.product.test_recommendation_actions",
        "test_an_unresolved_branch_is_conditional_not_a_fixed_value",
        ["C04", "C06", "UX-V03"],
        "the recommendation contract keeps unknown branches conditional",
        ["unknown stays conditional", "no value is invented for the reader"],
    ),
    "DEV-V05": (
        "tests.product.test_historical_configuration",
        "test_historical_record_is_an_independent_family_and_keeps_the_pointer",
        ["D03", "DEV-V05"],
        "a historical record is its own family and never moves the pointer",
        ["the current pointer is unchanged", "the historical family is separate"],
    ),
    "DEV-V06": (
        "tests.product.test_context",
        "test_installation_records_are_honest_about_unknown_paths",
        ["D-P01", "DEV-V06"],
        "an installation with an unknown path stays honestly unknown",
        ["no default path is invented", "the record is still usable"],
    ),
    "DEV-V07": (
        "tests.product.test_context",
        "test_distinct_unknown_entries_stay_independent_series",
        ["D-P02", "DEV-V07"],
        "two same-named entries without a located key stay separate",
        ["neither entry is merged", "each keeps its own series"],
    ),
    "DEV-V08": (
        "tests.product.test_information_answers",
        "test_facts_only_amendment_writes_no_source_task_rows",
        ["D-P04", "DEV-V08"],
        "a facts_only amendment never writes the source task",
        ["source task rows are unchanged", "the amendment is a new request"],
    ),
    "ENG-V07": (
        "tests.product.test_repository",
        "test_game_declaration_slot_encodes_unknown_platform_explicitly",
        ["D-P01", "ENG-V07"],
        "an unknown platform is encoded explicitly, not as a missing key",
        ["the slot is distinct from an absent platform", "no false merge occurs"],
    ),
    "UX-V02": (
        "tests.product.test_information_answers",
        "test_stale_versions_are_rejected_without_any_write",
        ["C11", "D04", "UX-V02"],
        "a stale answer is rejected with no partial write",
        ["the request is unchanged", "no revision is written"],
    ),
    "UX-V04": (
        "tests.product.test_adoption",
        "test_baseline_advances_the_pointer_and_records_the_declaration",
        ["C07", "D03", "UX-V04"],
        "a baseline advances the pointer and records its declaration",
        ["the pointer advances once", "the declaration is kept"],
    ),
    "UX-V05": (
        "tests.product.test_outcomes",
        "test_three_consistent_improvements_are_improved",
        ["D01", "D05", "UX-V05"],
        "three consistent improvements are reported as improved",
        ["the effect is improved", "acceptance stays a separate field"],
    ),
    "UX-V06": (
        "tests.product.test_metrics",
        "test_a_replay_returns_the_original_task_after_the_world_changed",
        ["D02", "UX-V06"],
        "a replay returns the original task even after the world changed",
        ["the same task is returned", "no second provider send"],
    ),
    "UX-V07": (
        "tests.product.test_adoption",
        "test_rollback_creates_a_new_version_that_restores_the_target",
        ["D03", "UX-V07"],
        "a manual restore creates a new version instead of rewriting history",
        ["a new version is created", "the older version is untouched"],
    ),
    "UX-V08": (
        "tests.product.test_log_association",
        "test_historical_association_creates_an_ended_session_and_closed_binding",
        ["D02", "D03", "UX-V08"],
        "a historical log association creates a closed session and binding",
        ["the session is ended", "the binding is closed"],
    ),
    "VAL-V03": (
        "tests.product.test_faults",
        "test_no_checkpoint_recovery_creates_one_child_and_does_not_resend",
        ["D02", "VAL-V03"],
        "a recovery with no checkpoint never resends the unknown call",
        ["one child task", "the send count is unchanged"],
    ),
    "VAL-V07": (
        "tests.product.test_comparisons",
        "test_frozen_before_sampling_allows_a_planned_comparison",
        ["D02", "D05", "VAL-V07"],
        "a protocol frozen before sampling allows a planned comparison",
        ["the frozen threshold is used", "no post-hoc downgrade"],
    ),
    "B-DAT-14": (
        "tests.product.test_repository",
        "test_legacy_bytes_and_idempotency_response_are_re_read_verbatim",
        ["B-DAT-14"],
        "legacy bytes and stored responses are re-read verbatim",
        ["the old bytes are unchanged", "the response is the original"],
    ),
    "B-STA-13": (
        "tests.product.test_information_answers",
        "test_a_resolved_request_rejects_a_direct_answer",
        ["B-STA-13"],
        "a resolved request cannot be answered directly again",
        ["the request state is enforced", "no source task write"],
    ),
}


def _application(tmp_path):
    repository, files, installation, context, device = setup(tmp_path)
    clock = FakeClock()
    application = Application(
        repository, file_root=str(tmp_path / "managed"), clock=clock,
    )
    return repository, application, installation, context, device, clock


def _session(application, installation, context, device, key="session"):
    from game_app.contracts.performance import CreateSessionRequest

    request = CreateSessionRequest.model_validate_json(canonical_json({
        "installation_id": installation,
        "device_snapshot_id": device,
        "game_context_id": context,
        "applied_id": None,
        "configuration_state": "unverified",
        "run_declaration": {
            "installation_id": installation,
            "fields": {},
            "raw_text": "",
        },
        "test_protocol_id": None,
        "recommendation_id": None,
    }))
    result = application.create_session(request, context_key(key))
    session_id = next(
        ref.entity_id for ref in result.refs if ref.entity_type == "PlaySession"
    )
    segment_id = next(
        ref.entity_id for ref in result.refs if ref.entity_type == "SessionSegment"
    )
    return session_id, segment_id


def _run_module_case(tmp_path, case_id, module_name, function_name):
    """Run one module acceptance test in its own temporary store."""

    directory = tmp_path / case_id
    directory.mkdir()

    def execute(_application):
        module = importlib.import_module(module_name)
        getattr(module, function_name)(directory)

    return execute


def _research_fixture(name: str) -> bytes:
    return (PRODUCT_FIXTURES / "research" / "v1" / name).read_bytes()


def _knw_v06(_application, tmp_path):
    directory = tmp_path / "knw-v06"
    directory.mkdir()
    repository = ProductRepository(directory / "product.sqlite3")
    repository.migrate()
    files = FileService(repository, directory / "managed")
    service = IdentityService(repository, files)
    page = service.list_games(GameCatalogQuery(query="", cursor=None, limit=20))
    assert page.choices == []
    assert page.next_cursor is None
    with repository.unit_of_work() as uow:
        assert uow.query(
            "SELECT COUNT(*) AS n FROM objects WHERE entity_type IN ('Game','GameSelection')"
        )[0]["n"] == 0


def _b_err_12(_application, tmp_path):
    """A malformed request id yields the stable envelope, not a traceback."""

    import contextlib
    import io

    from game_app.cli import main as cli_main

    directory = tmp_path / "b-err-12"
    directory.mkdir()
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = cli_main([
            "task", "get", "0" * 32,
            "--user-scope", "a" * 32,
            "--request-id", "NOT-HEX",
            "--db", str(directory / "product.sqlite3"),
            "--files", str(directory / "managed"),
        ])
    assert code == 2  # validation
    payload = json.loads(stderr.getvalue())
    assert payload["code"] == "INPUT_VALIDATION_FAILED"
    # The raw flag is never echoed into the envelope.
    assert "NOT-HEX" not in stderr.getvalue()
    assert not (directory / "product.sqlite3").exists()


def _cli_entry(_application, tmp_path):
    """The CLI is the product surface (Q1): the journey's entries must exist.

    This case checks the entry itself, not a module rule: every command a player
    needs to begin and finish a journey is registered, and an empty database
    still answers a catalog read without writing anything.
    """

    import contextlib
    import io

    from game_app.cli import build_parser, main as cli_main

    parser = build_parser()
    commands = next(
        action for action in parser._actions if action.dest == "command"
    ).choices
    expected = {
        "game": {"list", "select"},
        "installation": {"list", "register"},
        "local-setting": {"list", "register"},
        "device": {"scan"},
        "file": {"upload"},
        "knowledge": {"collect"},
        "import": {"run", "declare-identity"},
        "package": {"build", "get"},
        "session": {"create", "end-segment", "split", "end", "bind", "get"},
        "adopt": None,
        "analyze": None,
        "compare": None,
        "log": {"inspect", "associate"},
        "artifact": {"get"},
        "metric": {"list"},
        "test-protocol": {"create"},
        "comparison-protocol": {"create", "get"},
        "answer": None,
        "amendment": {"create"},
    }
    missing = [
        name for name in expected if name not in commands
    ]
    assert not missing, f"the CLI does not expose {missing}"
    for name, actions in expected.items():
        if actions is None:
            continue
        available = commands[name]._subparsers._group_actions[0].choices
        absent = actions - set(available)
        assert not absent, f"{name} is missing {sorted(absent)}"

    directory = tmp_path / "cli-entry"
    directory.mkdir()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli_main([
            "game", "list",
            "--db", str(directory / "product.sqlite3"),
            "--files", str(directory / "managed"),
            "--user-scope", "a" * 32,
        ])
    assert code == 0
    assert json.loads(out.getvalue())["choices"] == []


def _research_retained(_application, tmp_path):
    directory = tmp_path / "research-retained"
    directory.mkdir()
    source = directory / "fh6-extraction-results.json"
    source.write_bytes(_research_fixture(source.name))
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    repository = ProductRepository(directory / "product.sqlite3")
    repository.migrate()
    files = FileService(repository, directory / "managed")
    importer = ResearchImporter(repository, files)
    frozen = importer.freeze_document(
        source, user_scope_id="a" * 32, idempotency_key="research:retained"
    )
    result = importer.publish(frozen, user_scope_id="a" * 32)

    assert result.code == "retained_only"
    assert result.accepted_record_ids == []
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM objects WHERE entity_type=?",
                         ("ImportedRecord",))[0]["n"] == 0
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def _research_mapping(_application, tmp_path):
    payload = json.loads(
        _research_fixture("fh6-new-source-extraction.json").decode("utf-8")
    )
    mapping = map_research_document(
        payload,
        format_id=RESEARCH_SOURCE_SECTIONS_FORMAT_ID,
        snapshot_id="b" * 32,
        relative_name="fh6-new-source-extraction.json",
    )
    fps = next(
        record for record in mapping.records
        if record.payload["record_kind"] == "fps_interval"
    )
    fields = {field["product_field"]: field for field in fps.payload["fields"]}
    assert fields["fps_lower"]["value"] == 70
    assert fields["fps_upper"]["value"] == 85
    assert fields["fps_average"]["value"] is None
    assert fps.extension.boundary_semantics.status.value == "unknown"
    hdr = [
        record for record in mapping.records
        if record.payload["record_kind"] == "hdr_availability_observation"
    ]
    assert len(hdr) == 1
    assert all(
        "hdr" not in field["product_field"].lower()
        for record in mapping.records
        if record.payload["record_kind"] == "source_configuration_section"
        for field in record.payload["fields"]
    )
    assert {record.payload["format_id"] for record in mapping.records} == {
        RESEARCH_SOURCE_SECTIONS_FORMAT_ID
    }


# -- the report contract ------------------------------------------------------


def test_an_unexecuted_case_cannot_be_recorded_as_passed():
    with pytest.raises(ValidationError):
        CaseResult(
            case_id="unexecuted",
            fixture_version=1,
            run_id="run-1",
            status="passed",
            evidence_level="offline_fixture",
            observed_files=[],
            failure_notes=[],
            executed=False,
        )
    honest = CaseResult(
        case_id="unexecuted",
        fixture_version=1,
        run_id="run-1",
        status="not_run",
        evidence_level="offline_fixture",
        observed_files=[],
        failure_notes=["no module"],
        executed=False,
    )
    assert honest.status == "not_run"


def test_a_blocked_case_must_name_its_blocker():
    with pytest.raises(ValidationError):
        CaseResult(
            case_id="blocked",
            fixture_version=1,
            run_id="run-1",
            status="blocked",
            evidence_level="offline_fixture",
            observed_files=[],
            failure_notes=["not finished"],
            executed=False,
        )
    named = CaseResult(
        case_id="blocked",
        fixture_version=1,
        run_id="run-1",
        status="blocked",
        evidence_level="offline_fixture",
        observed_files=[],
        failure_notes=["needs real hardware"],
        executed=False,
        blocked_by="G-GAME",
    )
    assert named.blocked_by == "G-GAME"


def test_a_fully_skipped_run_is_not_a_pass():
    skipped = [
        CaseResult(
            case_id=f"case-{index}",
            fixture_version=1,
            run_id="run-1",
            status="not_run",
            evidence_level="offline_fixture",
            observed_files=[],
            failure_notes=["module missing"],
            executed=False,
        )
        for index in range(3)
    ]
    summary = summarize_results(skipped)
    assert summary["all_skipped"] is True
    assert summary["passed"] == 0
    assert summary["conclusion"] == "incomplete"
    with pytest.raises(InsufficientEvidence):
        conclude_results(skipped)


def test_a_new_run_appends_instead_of_overwriting_old_results(tmp_path):
    repository, application, installation, context, device, clock = _application(tmp_path)
    first = ScenarioRunner(application, repository, run_id="run-one")
    second = ScenarioRunner(application, repository, run_id="run-two")

    def always():
        return None

    for runner in (first, second):
        result = runner.run(
            case_id="SAME-CASE",
            requirement_ids=["VAL-V02"],
            purpose="a deterministic no-op probe",
            steps=["run"],
            expected_assertions=["it runs"],
            execute=lambda _app: always(),
            mock_components=["none"],
        )
        runner.write_report([result], tmp_path / "report")

    records = load_results(tmp_path / "report")
    assert {record["run"]["run_id"] for record in records} == {"run-one", "run-two"}
    assert len(records) == 2


# -- the matrix ---------------------------------------------------------------


def test_the_matrix_runs_existing_modules_and_records_every_result(tmp_path):
    repository, application, installation, context, device, clock = _application(tmp_path)
    runner = ScenarioRunner(application, repository, run_id="run-contracts")

    def perf_v01(app):
        session_id, segment_id = _session(app, installation, context, device, "v01")
        file_id, _ = publish_log(app.files, csv_bytes([10, 20, 30, 40]), "v01-log")
        artifact = craft_artifact(
            repository, session_id=session_id, segment_id=segment_id,
            file_id=file_id, offset=0.0, scale=0.001,
        )
        with pytest.raises(ContractError) as caught:
            app.analyze(
                analysis_input(artifact.id, BindingRef(
                    segment_id=segment_id, segment_revision=1, binding_revision=1,
                )),
                context_key("v01-analysis"),
            )
        assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
        counts = runner.counts()
        assert counts["PerformanceSummary"] == 0
        assert counts["external_calls"] == 0
        with repository.unit_of_work() as uow:
            assert uow.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 0

    def perf_v02(app):
        artifact, binding = artifact_from_csv(
            app.performance, app.files, installation, context, device,
            csv_bytes([10, 20, 30, 40]), "v02-log",
        )
        accepted = app.analyze(analysis_input(artifact.id, binding), context_key("v02"))
        assert app.run_once().task_id == accepted.task_id
        view = app.get_task(accepted.task_id)
        assert view.task.state.value == "completed"
        summary = view.task.result_refs[0].entity_id
        from game_app.contracts.performance import PerformanceSummary
        from tests.product.support.performance import load_object

        stored = load_object(app.performance, "PerformanceSummary", summary, PerformanceSummary)
        average = next(
            item for item in stored.metrics
            if item.numeric.number_kind != "unknown" and item.unit == "fps"
        )
        assert average.numeric.value == 40.0
        assert runner.counts()["PerformanceSummary"] == 1
        assert runner.counts()["external_calls"] == 0

    def perf_v09(app):
        catalog = app.list_metric_definitions(
            MetricCatalogQuery(frame_stream_kind=None)
        )
        assert len(catalog.choices) == 4
        assert all(choice.definition_id is None for choice in catalog.choices)
        assert runner.counts()["MetricDefinition"] == 0
        request = CreateTestProtocolRequest.model_validate_json(canonical_json({
            "game_context_id": context,
            "parent_protocol_id": None,
            "scenario": "route",
            "condition_fields": {},
            "controlled_variables": [],
            "metric_registry_version": "project-metrics/1",
            "metric_keys": ["average_present_rate/v1"],
            "warmup_seconds": None,
            "sample_duration_seconds": None,
            "repeat_count": None,
            "note": None,
        }))
        first = app.create_test_protocol(request, context_key("v09-protocol"))
        replay = app.create_test_protocol(request, context_key("v09-protocol"))
        assert [ref.entity_id for ref in replay.refs] == [
            ref.entity_id for ref in first.refs
        ]
        with pytest.raises(ContractError) as caught:
            app.create_test_protocol(
                CreateTestProtocolRequest.model_validate_json(canonical_json({
                    "game_context_id": context,
                    "parent_protocol_id": first.refs[0].entity_id,
                    "scenario": "route",
                    "condition_fields": {},
                    "controlled_variables": [],
                    "metric_registry_version": "project-metrics/1",
                    "metric_keys": ["average_present_rate/v1"],
                    "warmup_seconds": None,
                    "sample_duration_seconds": None,
                    "repeat_count": None,
                    "note": None,
                })),
                context_key("v09-protocol"),
            )
        assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
        counts = runner.counts()
        assert counts["TestProtocol"] == 1
        assert counts["MetricDefinition"] == 1
        assert counts["external_calls"] == 0

    results = [
        runner.run(
            case_id="PERF-V09",
            requirement_ids=["D-P03", "PERF-V09"],
            purpose="the protocol entry is idempotent and writes no catalog GET",
            steps=["GET the metric catalog", "create a protocol", "replay the same key"],
            expected_assertions=[
                "four packaged choices with no definition id",
                "one TestProtocol and one MetricDefinition",
                "same key returns the same refs; different content is a 409",
            ],
            execute=perf_v09,
            initial_fixture="smoke-inputs@1",
            input_refs=["packaged project-metrics/1"],
            mock_components=["time stub"],
            observations=["external_calls=0", "provider_sends=0"],
        ),
        runner.run(
            case_id="PERF-V01",
            requirement_ids=["D03", "PERF-V01"],
            purpose="an open segment/binding cannot be analyzed",
            steps=["create a session", "submit analyze with the open revision"],
            expected_assertions=[
                "PRECONDITION_UNVERIFIED",
                "PerformanceSummary count is 0",
                "task count is 0 and the key is not consumed",
                "external_calls is 0",
            ],
            execute=perf_v01,
            input_refs=["synthetic CSV"],
            mock_components=["time stub", "offline format registry"],
            observations=["external_calls=0", "provider_sends=0"],
        ),
        runner.run(
            case_id="PERF-V02",
            requirement_ids=["D05", "PERF-V02"],
            purpose="the original formula on [10,20,30,40]",
            steps=["associate a synthetic log", "run analysis"],
            expected_assertions=[
                "average_present_rate == 40 (exact integer metric)",
                "one PerformanceSummary",
                "external_calls is 0",
            ],
            execute=perf_v02,
            input_refs=["synthetic CSV"],
            mock_components=["time stub", "offline format registry"],
            observations=["external_calls=0", "provider_sends=0"],
        ),
        runner.run(
            case_id="IMP-V04-research-retained",
            requirement_ids=["C08", "IMP-V04"],
            purpose="an unannotated research document stays retained_only",
            steps=["freeze a research copy", "publish without an independent annotation"],
            expected_assertions=[
                "result code is retained_only",
                "zero ImportedRecord",
                "the original bytes are unchanged",
                "external_calls is 0",
            ],
            execute=lambda _app: _research_retained(_app, tmp_path),
            input_refs=["fh6-extraction-results.json@1"],
            mock_components=["offline format registry"],
            observations=["external_calls=0", "provider_sends=0"],
        ),
        runner.run(
            case_id="IMP-V04-research-mapping",
            requirement_ids=["C08", "IMP-V04"],
            purpose="the mapping keeps bounds, HDR and sections separate",
            steps=["map the frozen new-source extraction"],
            expected_assertions=[
                "fps lower/upper are 70/85 and the average stays null",
                "boundary semantics stay unknown",
                "HDR is its own observation",
                "external_calls is 0",
            ],
            execute=lambda _app: _research_mapping(_app, tmp_path),
            input_refs=["fh6-new-source-extraction.json@1"],
            mock_components=["offline format registry"],
            observations=["external_calls=0", "provider_sends=0"],
        ),
        runner.run(
            case_id="UX-V01-cli-entry",
            requirement_ids=["C02", "C03", "UX-V01"],
            purpose="the CLI exposes every entry a journey needs",
            steps=["build the parser", "read the catalog from an empty database"],
            expected_assertions=[
                "every journey command and action is registered",
                "an empty catalog answers with no choices and exit 0",
            ],
            execute=lambda _app: _cli_entry(_app, tmp_path),
            input_refs=["command line"],
            mock_components=["CLI entry over a temporary store"],
            observations=["external_calls=0", "provider_sends=0"],
        ),
        runner.run(
            case_id="KNW-V06",
            requirement_ids=["D-P01", "C02", "KNW-V06"],
            purpose="an empty catalog is read without writing anything",
            steps=["GET the empty game catalog"],
            expected_assertions=[
                "the page is empty with no cursor",
                "no Game or GameSelection is written",
                "external_calls is 0",
            ],
            execute=lambda _app: _knw_v06(_app, tmp_path),
            input_refs=["empty catalog"],
            mock_components=["local identity service"],
            observations=["external_calls=0", "provider_sends=0"],
        ),
        runner.run(
            case_id="B-ERR-12",
            requirement_ids=["B-ERR-12"],
            purpose="a malformed flag yields the shared error envelope",
            steps=["run the CLI with a malformed request id"],
            expected_assertions=[
                "exit code 2 (validation)",
                "the envelope code is INPUT_VALIDATION_FAILED",
                "the raw flag is not echoed and no database is created",
            ],
            execute=lambda _app: _b_err_12(_app, tmp_path),
            input_refs=["CLI argument vector"],
            mock_components=["CLI entry over a temporary store"],
            observations=["external_calls=0", "provider_sends=0"],
        ),
    ]

    for case_id, (module_name, function_name, requirements, purpose, assertions) in (
        MODULE_CASES.items()
    ):
        results.append(runner.run(
            case_id=case_id,
            requirement_ids=requirements,
            purpose=purpose,
            steps=[f"run {module_name}.{function_name} in a temporary store"],
            expected_assertions=assertions,
            execute=_run_module_case(tmp_path, case_id, module_name, function_name),
            input_refs=[f"{module_name}::{function_name}"],
            mock_components=[f"module test {module_name}.{function_name}"],
            observations=[
                f"module_test={module_name}::{function_name}",
                "external_calls=0 (module store is separate)",
            ],
        ))

    results.extend([
        runner.not_run(
            case_id="PERF-V06",
            requirement_ids=["D02", "PERF-V06"],
            reason="no real capture sample exists in this environment: no"
                   " PresentMon CSV, version or options were available, so the"
                   " fixed-version format registration cannot be verified",
            blocked=True,
            blocked_by="G-FORMAT (no real sample)",
        ),
        runner.not_run(
            case_id="VAL-V04",
            requirement_ids=["C04", "C08", "VAL-V04"],
            reason="no independent annotation exists; only a same-session model"
                   " recheck is available (F-R3)",
            blocked=True,
            blocked_by="no independent annotation",
        ),
        runner.not_run(
            case_id="VAL-V05",
            requirement_ids=["C06", "D05", "VAL-V05"],
            reason="needs a real game and player rounds; the evidence level here"
                   " is offline_fixture (F-R12)",
            blocked=True,
            blocked_by="G-EFFECT (no real game)",
        ),
        runner.not_run(
            case_id="VAL-V06",
            requirement_ids=["C02", "C11", "D02", "VAL-V06"],
            reason="the C5 page journey needs a browser and an HTTP surface;"
                   " Q1 fixed the product as CLI-only",
            blocked=True,
            blocked_by="Q1 (CLI only)",
        ),
        runner.not_run(
            case_id="UX-V09",
            requirement_ids=["D-P01", "D-P04", "UX-V09"],
            reason="the empty-library journey is a page flow; Q1 fixed the"
                   " product as CLI-only",
            blocked=True,
            blocked_by="Q1 (CLI only)",
        ),
        runner.not_run(
            case_id="VAL-V08",
            requirement_ids=["D-P01", "D-P04", "VAL-V08"],
            reason="the D-P journey needs a browser; Q1 fixed the product as CLI-only",
            blocked=True,
            blocked_by="Q1 (CLI only)",
        ),
        runner.not_run(
            case_id="VAL-V01",
            requirement_ids=["C10", "VAL-V01"],
            reason="VAL-V01 checks a design-only change set; this batch changed"
                   " product code, so that boundary check does not apply to it"
                   " (the original-bytes clause is asserted by the module tests)",
        ),
    ])

    # VAL-V02 is the closure of the whole matrix, so its blocker is whatever
    # still blocks the cases it closes — named, not "not finished".
    remaining = sorted({
        result.blocked_by for result in results
        if result.status == "blocked" and result.blocked_by
    })
    registrations = len({_family(result.case_id) for result in results}) + 1
    # VAL-V02 is the closure case itself, so it is counted here rather than
    # being allowed to stand as its own evidence.
    results.append(runner.not_run(
        case_id="VAL-V02-full",
        requirement_ids=["VAL-V02"],
        reason=(
            f"{registrations} of {len(REQUIRED_CASES)} required case families are"
            " registered and every one that can run offline did run; full"
            " closure still waits on: " + "; ".join(remaining)
        ),
        blocked=True,
        blocked_by="; ".join(remaining),
    ))

    report = runner.write_report(results, tmp_path / "report")
    records = load_results(tmp_path / "report")

    assert report.is_file()
    assert len(records) == len(results)
    statuses = {record["result"]["case_id"]: record["result"]["status"] for record in records}
    # Coverage is checked per case family so a suffixed id cannot stand in for
    # a family that was never registered.
    registered = {_family(result.case_id) for result in results}
    for case_id in REQUIRED_CASES:
        assert case_id in registered, f"matrix is missing {case_id}"
    for case_id in F1_CASES | CLI_CASES:
        assert statuses[case_id] == "passed"
    assert registered == REQUIRED_CASES, sorted(registered - REQUIRED_CASES)
    assert statuses["PERF-V06"] == "blocked"
    assert statuses["VAL-V02-full"] == "blocked"
    # The closure case names the real gates, never "not finished".
    closure = next(
        result for result in results if result.case_id == "VAL-V02-full"
    )
    assert "module owners" not in (closure.blocked_by or "")
    assert "G-FORMAT" in (closure.blocked_by or "")

    summary = conclude_results([record["result"] for record in records])
    assert summary["passed"] > 0
    assert len(summary["rows"]) == len(results)
    blocked = [gap for gap in summary["gaps"] if gap["status"] == "blocked"]
    assert blocked and all(gap["blocked_by"] for gap in blocked)

    for record in records:
        result = record["result"]
        if result["status"] == "passed":
            assert result["executed"] is True
            assert result["requirement_ids"]
            assert result["steps"]
            assert result["expected_assertions"]
            assert result["mock_components"]
            assert result["observations"]
        else:
            assert result["failure_notes"]
        assert record["run"]["run_id"] == "run-contracts"
        assert record["run"]["git_revision"]
        assert "no HTTP" in record["run"]["environment_note"]
