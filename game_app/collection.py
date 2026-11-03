"""KNW-T03: planned follow-up collection and the standard frozen group.

Two components live here and nothing else:

* :class:`FollowupPlanner` orders open gaps by priority and hands out at most
  ``max_queries_per_gap`` queries per gap, one round at a time.
* :class:`CollectionSnapshotWriter` writes a ``collector-run/v0.1`` compatible
  file group into managed storage and registers the run's identity mapping, so
  02a can import it as ``origin=collector_run`` without touching the legacy CLI.

No real provider is enabled.  The handler in :mod:`game_app.knowledge` talks to
the provider carried by the task service; the reply shapes parsed here are the
**stub boundary** used by tests (``urls`` for a search, ``records`` for an
extraction) and are never claimed to be a vendor protocol.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from game_app.contracts.common import CheckResult
from game_app.contracts.enums import CheckStatus, CollectionStopReason, CollectionResult
from game_app.contracts.knowledge import IdentityMapping
from game_app.contracts.primitives import utc_now
from game_app.errors import ContractError, ErrorCode, FieldError
from game_app.files import FileEntry, FileManifest, FileService
from game_app.importer import (
    COLLECTOR_REQUIRED_FILES,
    COLLECTOR_RUN_FORMAT_ID,
    COLLECTOR_RECORD_GROUPS,
    FormatInspection,
    FrozenGroup,
    default_format_registry,
)
from game_app.repository import ProductRepository, canonical_json

PRODUCT_CONTRACT_VERSION = "product-contract/0.4"
COLLECTION_RULE_VERSION = "collection/0.4"
_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}


class CollectionProtocolError(Exception):
    """A provider reply does not have the stub protocol's shape."""


# --- planning ----------------------------------------------------------------


@dataclass(frozen=True)
class GapSpec:
    """One gap the planner may query; ``expects`` closes it on a gain."""

    key: str
    priority: str
    operation_scope: str
    question: str
    expects: tuple[str, ...]

    @property
    def order(self) -> int:
        return _PRIORITY_ORDER.get(self.priority, 3)


@dataclass(frozen=True)
class PlannedQuery:
    gap_key: str
    operation_scope: str
    priority: str
    query: str
    expects: tuple[str, ...]


class FollowupPlanner:
    """Order gaps by P0/P1/P2 and bound the queries one gap may consume."""

    def __init__(self, *, max_queries_per_gap: int = 2) -> None:
        self.max_queries_per_gap = max_queries_per_gap

    def ordered(self, gaps: Iterable[GapSpec]) -> list[GapSpec]:
        return sorted(gaps, key=lambda gap: (gap.order, gap.key))

    def plan_round(
        self, gaps: Iterable[GapSpec], *, used: Mapping[str, int]
    ) -> list[PlannedQuery]:
        planned: list[PlannedQuery] = []
        for gap in self.ordered(gaps):
            if used.get(gap.key, 0) >= self.max_queries_per_gap:
                continue
            planned.append(PlannedQuery(
                gap_key=gap.key,
                operation_scope=gap.operation_scope,
                priority=gap.priority,
                query=gap.question,
                expects=gap.expects,
            ))
        return planned


# --- findings ----------------------------------------------------------------


@dataclass
class CollectedRecord:
    local_id: str
    record_type: str
    payload: dict[str, Any]
    recommendation_status: str | None = None


@dataclass
class CollectedSource:
    scope_id: str
    url: str
    revision_id: str
    retrieved_at: str
    model_requested: str | None
    raw_extraction: dict[str, Any]
    records: list[CollectedRecord] = field(default_factory=list)


@dataclass(frozen=True)
class CollectedGap:
    gap_id: str
    priority: str
    question: str
    reason: str
    field: str
    status: str = "open"
    source_ids: tuple[str, ...] = ()


def canonical_url(url: str) -> str:
    """A minimal, deterministic URL key for de-duplicating one read per URL."""
    return url.strip()


def source_scope_id(url: str) -> str:
    return "src-" + hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:16]


def source_revision_id(url: str) -> str:
    return "rev-" + hashlib.sha256(
        ("revision:" + canonical_url(url)).encode("utf-8")
    ).hexdigest()[:16]


def record_local_id(record_type: str, payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(payload)).hexdigest()[:10]
    return f"{record_type[:3]}-{digest}"


# --- reply parsing (stub boundary) ------------------------------------------


def parse_search_reply(reply: Any) -> list[str]:
    """``{"urls": [str, ...]}`` — the stub search reply."""
    if not isinstance(reply, dict):
        raise CollectionProtocolError("the search reply is not an object")
    urls = reply.get("urls")
    if not isinstance(urls, list):
        raise CollectionProtocolError("the search reply has no urls array")
    return [
        url for url in urls if isinstance(url, str) and url.strip()
    ]


def parse_extraction_reply(
    reply: Any, *, scope_id: str
) -> tuple[list[CollectedRecord], list[str]]:
    """``{"records": [{"record_type", "payload", "local_id"?}]}``.

    A malformed record is one error; it never causes a second read of the same
    URL.  Several bad records therefore produce several errors from one read.
    """
    if not isinstance(reply, dict):
        raise CollectionProtocolError("the extraction reply is not an object")
    rows = reply.get("records")
    if not isinstance(rows, list):
        raise CollectionProtocolError("the extraction reply has no records array")
    records: list[CollectedRecord] = []
    errors: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"{scope_id} record[{index}]: not an object")
            continue
        record_type = row.get("record_type")
        payload = row.get("payload")
        if record_type not in COLLECTOR_RECORD_GROUPS:
            errors.append(
                f"{scope_id} record[{index}]: unregistered type {record_type!r}"
            )
            continue
        if not isinstance(payload, dict):
            errors.append(f"{scope_id} record[{index}]: payload is not an object")
            continue
        local_id = row.get("local_id")
        if not isinstance(local_id, str) or not local_id.strip():
            local_id = record_local_id(str(record_type), payload)
        records.append(CollectedRecord(
            local_id=local_id,
            record_type=str(record_type),
            payload=payload,
            recommendation_status=(
                row.get("recommendation_status")
                if isinstance(row.get("recommendation_status"), str)
                else ("candidate" if record_type == "configurations" else None)
            ),
        ))
    return records, errors


# --- the standard group ------------------------------------------------------


class CollectionSnapshotWriter:
    """Write one ``collector-run/v0.1`` group and register its identity.

    The bytes are canonical JSON over the collected findings, so a retry with
    the same task reproduces the same file ids and the same fingerprint.  No
    clock value enters the group: a crash followed by a retry must not make a
    different "same" run.
    """

    def __init__(
        self,
        repository: ProductRepository,
        files: FileService,
        *,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.repository = repository
        self.files = files
        self.clock = clock

    def write(
        self,
        *,
        task_id: str,
        run_id: str,
        game_series_id: str,
        user_scope_id: str,
        started_at: str,
        collection_result: CollectionResult,
        stop_reason: CollectionStopReason | None,
        sources: Sequence[CollectedSource],
        gaps: Sequence[CollectedGap],
    ) -> tuple[FrozenGroup, str]:
        legacy_game_id = game_series_id
        members = {
            "run.json": canonical_json({
                "id": run_id,
                "contract_version": "0.1",
                "started_at": started_at,
                "finished_at": None,
                "input": {
                    "game_name": legacy_game_id,
                    "mode": "followup",
                    "platform_scope": None,
                    "seed_urls": [],
                },
                "game_id": legacy_game_id,
                "status": collection_result.value,
                "stop_reason": None if stop_reason is None else stop_reason.value,
                "interrupted": False,
            }),
            "sources.json": canonical_json([
                {
                    "id": source.scope_id,
                    "revision_id": source.revision_id,
                    "url": source.url,
                    "retrieved_at": source.retrieved_at,
                    "role": "followup_collection",
                    "status": "extracted",
                    "lineage_status": "unknown",
                }
                for source in sorted(sources, key=lambda item: item.scope_id)
            ]),
            "records.json": canonical_json([
                {
                    "id": f"{source.scope_id}:{record.local_id}",
                    "local_id": record.local_id,
                    "record_type": record.record_type,
                    "record_status": "accepted",
                    "recommendation_status": record.recommendation_status,
                    "applicability": "unknown",
                    "reference_scope": source.scope_id,
                    "source_id": source.scope_id,
                    "revision_id": source.revision_id,
                    "game_id": legacy_game_id,
                    "payload": record.payload,
                }
                for source in sorted(sources, key=lambda item: item.scope_id)
                for record in sorted(source.records, key=lambda item: item.local_id)
            ]),
            "gaps.json": canonical_json([
                {
                    "id": gap.gap_id,
                    "priority": gap.priority,
                    "field": gap.field,
                    "reason": gap.reason,
                    "question": gap.question,
                    "status": gap.status,
                    "source_ids": list(gap.source_ids),
                    "record_ids": [],
                }
                for gap in sorted(gaps, key=lambda item: item.gap_id)
            ]),
            "extractions.json": canonical_json([
                {
                    "source_id": source.scope_id,
                    "status": "ok" if source.records else "empty",
                    "model": source.model_requested,
                    "started_at": started_at,
                    "raw": source.raw_extraction,
                    "parsed": {"records": len(source.records)},
                }
                for source in sorted(sources, key=lambda item: item.scope_id)
            ]),
        }
        inspection = default_format_registry().inspect_files(members)
        if inspection.format_id != COLLECTOR_RUN_FORMAT_ID:
            raise ContractError(
                ErrorCode.INTERNAL_ERROR,
                "the collection writer produced a group the importer does not"
                " recognise: " + "; ".join(inspection.limitations),
            )
        file_ids = {
            name: self.files.store_artifact(
                members[name],
                relative_name=f"collections/{task_id}/{name}",
                stable_key=f"collection:{task_id}:{name}",
            )
            for name in COLLECTOR_REQUIRED_FILES
        }
        entries = [
            _entry(name, members[name]) for name in sorted(members)
        ]
        mapping_id = self._register_identity(
            run_id=run_id,
            legacy_game_id=legacy_game_id,
            target_game_id=game_series_id,
            user_scope_id=user_scope_id,
        )
        return (
            FrozenGroup(
                format_id=COLLECTOR_RUN_FORMAT_ID,
                operation_id="",
                file_ids=file_ids,
                manifest=FileManifest(entries=entries),
                inspection=FormatInspection(
                    format_id=COLLECTOR_RUN_FORMAT_ID,
                    required_files=list(COLLECTOR_REQUIRED_FILES),
                    missing_files=[],
                    limitations=list(inspection.limitations),
                ),
            ),
            mapping_id,
        )

    def _register_identity(
        self,
        *,
        run_id: str,
        legacy_game_id: str,
        target_game_id: str,
        user_scope_id: str,
    ) -> str:
        namespace = f"collector_run:{run_id}"
        with self.repository.unit_of_work() as uow:
            existing = [
                IdentityMapping.model_validate_json(stored.payload_bytes)
                for stored in uow.iter_objects("IdentityMapping")
                if stored.payload.get("legacy_namespace") == namespace
                and stored.payload.get("legacy_value") == legacy_game_id
            ]
        if existing:
            mapping = existing[0]
            if mapping.target_game_id != target_game_id:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "this run already names another game; the mapping is not"
                    " overwritten",
                )
            return mapping.id
        mapping = IdentityMapping(
            id=_mapping_id(namespace, legacy_game_id),
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=self.clock(),
            legacy_namespace=namespace,
            legacy_value=legacy_game_id,
            target_game_id=target_game_id,
            declared_by_user_scope_id=user_scope_id,
            checks=[CheckResult(
                check_id="identity.task_context",
                definition_version=COLLECTION_RULE_VERSION,
                status=CheckStatus.pass_,
                input_refs=[],
                evidence_refs=[],
                checked_at=self.clock(),
                method="task context",
                note="the run was collected for the task's fixed game context;"
                " this is not an externally verified identity",
            )],
        )
        with self.repository.unit_of_work() as uow:
            uow.insert_object("IdentityMapping", mapping)
        return mapping.id


def _mapping_id(namespace: str, legacy_game_id: str) -> str:
    return hashlib.sha256(f"{namespace}:{legacy_game_id}".encode("utf-8")).hexdigest()[:32]


def _entry(name: str, data: bytes) -> FileEntry:
    return FileEntry(
        relative_name=name,
        byte_length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        media_type="application/json",
    )


__all__ = [
    "COLLECTION_RULE_VERSION",
    "CollectedGap",
    "CollectedRecord",
    "CollectedSource",
    "CollectionProtocolError",
    "CollectionSnapshotWriter",
    "FollowupPlanner",
    "GapSpec",
    "PlannedQuery",
    "canonical_url",
    "parse_extraction_reply",
    "parse_search_reply",
    "record_local_id",
    "source_revision_id",
    "source_scope_id",
]
