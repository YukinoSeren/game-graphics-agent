"""Shared fixtures for the adoption and historical-configuration tests.

These are test stubs for records whose owners (KNW/PERF/REC/FBK) are not built
yet: the installation is a real contract object, while the context, snapshot,
ChangeSet and segment are stored JSON stubs with only the fields the service
reads.  No product code path creates them.
"""

from __future__ import annotations

from pathlib import Path

from game_app.contracts.common import FieldValue, MutationContext
from game_app.contracts.configuration import AdoptConfigurationInput
from game_app.contracts.device import GameInstallation
from game_app.contracts.enums import AcquisitionStatus, ValueState
from game_app.contracts.primitives import utc_now
from game_app.files import FileService
from game_app.repository import ProductRepository, canonical_json

USER = "a" * 32
GAME_ID = "b" * 32


def uid(value: int) -> str:
    return f"{value:032x}"


def context_key(key: str = "key-1") -> MutationContext:
    return MutationContext(user_scope_id=USER, idempotency_key=key, request_id=uid(999))


def unknown_platform() -> FieldValue[str]:
    return FieldValue[str](
        value_state=ValueState.unknown,
        value=None,
        unknown_reason="not_provided",
        acquisition_status=AcquisitionStatus.not_attempted,
    )


def store_raw(repository: ProductRepository, entity_type: str, payload: dict) -> str:
    with repository.unit_of_work() as uow:
        uow.insert_object_bytes(
            entity_type,
            schema_version="product-contract/0.4",
            raw=canonical_json(payload),
            object_id=payload["id"],
        )
    return payload["id"]


def setup(tmp_path: Path):
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    files = FileService(repository, tmp_path / "managed")
    installation_id = uid(101)
    with repository.unit_of_work() as uow:
        uow.insert_versioned("GameInstallation", GameInstallation(
            id=installation_id,
            schema_version="product-contract/0.4",
            created_at=utc_now(),
            created_by_task_id=None,
            series_id=installation_id,
            version=1,
            revision=1,
            user_scope_id=USER,
            game_id=GAME_ID,
            origin="user_provided",
            platform=unknown_platform(),
            private_path_ref=None,
        ))
    context_id = store_raw(repository, "GameContext", {
        "id": uid(102),
        "installation_id": installation_id,
        "game_id": GAME_ID,
    })
    device_id = store_raw(repository, "DeviceSnapshot", {"id": uid(103)})
    return repository, files, installation_id, context_id, device_id


class Definitions:
    """Registers local setting definitions through the real catalog primitive."""

    def __init__(self, repository: ProductRepository, installation_id: str) -> None:
        self.repository = repository
        self.installation_id = installation_id
        self._next = 200

    def new(self, raw_name: str, menu_path=None, *, series: str | None = None) -> tuple[str, str]:
        definition_id = uid(self._next)
        series_id = series or uid(self._next + 1)
        self._next += 2
        with self.repository.unit_of_work() as uow:
            uow.register_local_setting(
                installation_id=self.installation_id,
                namespace="player",
                raw_name=raw_name,
                menu_path=menu_path if menu_path is not None else ["图形", raw_name],
                local_setting_id=definition_id,
                series_id=series_id,
            )
        return definition_id, series_id

    def correct(
        self, old_definition_id: str, series_id: str, raw_name: str, menu_path=None
    ) -> str:
        definition_id = uid(self._next)
        self._next += 1
        with self.repository.unit_of_work() as uow:
            head = uow.list_local_setting_heads(self.installation_id)
            version = next(
                item.version for item in head if item.local_setting_id == old_definition_id
            )
            uow.register_local_setting(
                installation_id=self.installation_id,
                namespace="player",
                raw_name=raw_name,
                menu_path=menu_path if menu_path is not None else ["图形", raw_name],
                local_setting_id=definition_id,
                series_id=series_id,
                version=version + 1,
                parent_definition_id=old_definition_id,
            )
        return definition_id


def declaration(
    setting_id: str,
    *,
    value=None,
    unit: str | None = None,
    activation: str = "unknown",
    unknown_reason: str | None = None,
) -> dict:
    if value is None and unknown_reason is None:
        unknown_reason = "not_provided"
    return {
        "setting_ref": {"kind": "local", "local_setting_id": setting_id},
        "value": value,
        "unknown_reason": unknown_reason,
        "unit": unit,
        "claimed_activation": activation,
        "activation_note": None,
        "observation_id": None,
    }


def adopt_request(
    installation_id: str,
    context_id: str,
    device_id: str,
    **overrides,
) -> AdoptConfigurationInput:
    payload = {
        "kind": "adopt_configuration",
        "origin": "baseline",
        "installation_id": installation_id,
        "device_snapshot_id": device_id,
        "game_context_id": context_id,
        "expected_current_applied_id": None,
        "recommendation_id": None,
        "base_applied_id": None,
        "change_set_id": None,
        "restored_from_applied_id": None,
        "declared_settings": [],
        "preset_reference": None,
        "claimed_activation": "unknown",
        "setting_ref_replacements": [],
    }
    payload.update(overrides)
    # External input arrives as JSON; validating the JSON form keeps the enum
    # fields strict without accepting Python strings everywhere.
    return AdoptConfigurationInput.model_validate_json(canonical_json(payload))
