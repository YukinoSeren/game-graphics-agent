"""Load and validate the two isolated demo scenario packs.

Temporary presentation code only. Nothing here may be used by formal
development as a design, contract, or evidence source.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]

SCHEMA = "demo-fixtures/1"
SCENARIO_IDS = ("mw2-rtx4060", "fh6-rtx5090")
ACTIVE_SCENARIO_IDS = ("mw2-rtx4060",)
MEMBERS = (
    "context.json",
    "sources.json",
    "settings.json",
    "templates.json",
    "rules.json",
    "trace-recipes.json",
)
TEMPLATE_KEYS = ("baseline", "main", "quality", "performance")
RECIPE_LABELS = ("baseline", "recommended", "adjusted", "no_gain", "short")
MAX_FIXTURE_BYTES = 1024 * 1024


class ScenarioError(ValueError):
    """A fixture pack is missing, malformed, or crosses case boundaries."""


def _reject_constant(value: str) -> None:
    raise ScenarioError(f"fixture contains a non-finite number: {value}")


def _read_json(path: Path) -> JsonObject:
    try:
        raw = path.read_bytes()
    except OSError as exc:  # pragma: no cover - filesystem failure path
        raise ScenarioError(f"cannot read fixture member: {path.name}") from exc
    if len(raw) > MAX_FIXTURE_BYTES:
        raise ScenarioError(f"fixture member exceeds the size limit: {path.name}")
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScenarioError(f"fixture member is not valid UTF-8 JSON: {path.name}") from exc
    if not isinstance(data, dict):
        raise ScenarioError(f"fixture member must be a JSON object: {path.name}")
    return data


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScenarioError(message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_demo_only(data: JsonObject, scenario_id: str) -> None:
    _require(data.get("schema") == SCHEMA, f"{scenario_id}: unexpected fixture schema")
    _require(data.get("demo_only") is True, f"{scenario_id}: demo_only must be true")
    _require(data.get("scenario_id") == scenario_id, f"{scenario_id}: scenario_id mismatch")


def _validate_context(scenario_id: str, context: JsonObject) -> None:
    _check_demo_only(context, scenario_id)
    for key in ("game_name", "gpu_name"):
        _require(
            isinstance(context.get(key), str) and context[key].strip() != "",
            f"{scenario_id}: {key} must be a non-empty string",
        )
    resolution = context.get("resolution")
    _require(isinstance(resolution, dict), f"{scenario_id}: resolution is required")
    _require(
        _is_int(resolution.get("width")) and resolution["width"] > 0,
        f"{scenario_id}: resolution width must be a positive integer",
    )
    _require(
        _is_int(resolution.get("height")) and resolution["height"] > 0,
        f"{scenario_id}: resolution height must be a positive integer",
    )
    unknown = context.get("unknown_fields")
    _require(isinstance(unknown, list), f"{scenario_id}: unknown_fields must be a list")
    for field in unknown:
        _require(isinstance(field, str), f"{scenario_id}: unknown field names must be strings")
        _require(
            context.get(field) is None,
            f"{scenario_id}: unknown field {field} must stay null in the preset",
        )


def _validate_sources(scenario_id: str, sources: list[JsonObject], *, pack_version=None) -> dict[str, JsonObject]:
    _require(len(sources) >= 4, f"{scenario_id}: at least four demo sources are required")
    by_id: dict[str, JsonObject] = {}
    repost_seen = False
    for source in sources:
        source_id = source.get("id")
        _require(
            isinstance(source_id, str) and source_id.startswith(f"demo://{scenario_id}/"),
            f"{scenario_id}: source ids must use the demo:// scheme and case prefix",
        )
        _require(source_id not in by_id, f"{scenario_id}: duplicate source id {source_id}")
        _require(
            isinstance(source.get("title"), str) and source["title"].strip() != "",
            f"{scenario_id}: source {source_id} needs a title",
        )
        _require(
            isinstance(source.get("author_label"), str) and "模拟" in source["author_label"],
            f"{scenario_id}: source {source_id} must be labelled as a simulated author",
        )
        _require(
            source.get("relationship") in ("original", "repost"),
            f"{scenario_id}: source {source_id} has an unsupported relationship",
        )
        if source.get("relationship") == "repost":
            repost_seen = True
        _require(
            isinstance(source.get("content_blocks"), list) and source["content_blocks"],
            f"{scenario_id}: source {source_id} needs content",
        )
        _require(
            isinstance(source.get("limitations"), str) and source["limitations"].strip() != "",
            f"{scenario_id}: source {source_id} must state its limitations",
        )
        by_id[source_id] = source
    if pack_version != "mw1-richness/2":
        _require(repost_seen, f"{scenario_id}: one demo source must demonstrate a repost relationship")
    else:
        _require(len(sources) == 15, "richness pack must contain exactly fifteen sources")
        _require({s.get("source_key") for s in sources} == {f"S{i:02}" for i in range(1, 16)}, "richness source keys are incomplete")
        _require(len({s.get("channel") for s in sources}) == 6, "richness pack must cover six channels")
        for source in sources:
            body = "".join(block.get("text", "") for block in source.get("display_blocks", []))
            _require(len(body) >= 300, f"{source['id']}: source body is incomplete")
            _require(source.get("external_url") is None and source.get("data_origin") == "simulated", "local sources must retain their origin")
    for source in sources:
        for related in source.get("related_ids", []):
            _require(
                related in by_id,
                f"{scenario_id}: source {source['id']} references missing source {related}",
            )
    return by_id


def _validate_settings(
    scenario_id: str, settings: list[JsonObject], sources: dict[str, JsonObject]
) -> dict[str, JsonObject]:
    _require(len(settings) >= 12, f"{scenario_id}: at least twelve settings are required")
    by_id: dict[str, JsonObject] = {}
    required = (
        "id",
        "display_name",
        "demo_original_name",
        "group",
        "type",
        "allowed_values",
        "recommended_value",
        "quality_variant",
        "performance_variant",
        "reason",
        "evidence_source_ids",
        "dependencies",
        "needs_verification",
        "unknown",
    )
    for setting in settings:
        setting_id = setting.get("id")
        _require(
            isinstance(setting_id, str) and setting_id.startswith(f"{scenario_id.split('-')[0]}."),
            f"{scenario_id}: setting ids must use the case prefix",
        )
        _require(setting_id not in by_id, f"{scenario_id}: duplicate setting id {setting_id}")
        for key in required:
            _require(key in setting, f"{scenario_id}: setting {setting_id} is missing {key}")
        _require(
            setting["type"] in ("quality", "fixed", "unknown"),
            f"{scenario_id}: setting {setting_id} has an unsupported type",
        )
        _require(
            isinstance(setting["allowed_values"], list),
            f"{scenario_id}: setting {setting_id} allowed_values must be a list",
        )
        _require(
            isinstance(setting["evidence_source_ids"], list) and setting["evidence_source_ids"],
            f"{scenario_id}: setting {setting_id} needs at least one evidence source",
        )
        for source_id in setting["evidence_source_ids"]:
            _require(
                source_id in sources,
                f"{scenario_id}: setting {setting_id} references unknown source {source_id}",
            )
        if setting["type"] == "unknown" or setting["unknown"]:
            _require(
                setting["recommended_value"] is None,
                f"{scenario_id}: unknown setting {setting_id} cannot carry a recommended value",
            )
        else:
            _require(
                setting["recommended_value"] in setting["allowed_values"],
                f"{scenario_id}: setting {setting_id} recommended value is outside its catalog",
            )
        by_id[setting_id] = setting
    return by_id


def _validate_templates(
    scenario_id: str, templates: JsonObject, settings: dict[str, JsonObject]
) -> None:
    for key in TEMPLATE_KEYS:
        _require(key in templates, f"{scenario_id}: template {key} is missing")
        template = templates[key]
        _require(isinstance(template, dict), f"{scenario_id}: template {key} must be an object")
        values = template.get("values")
        _require(isinstance(values, dict), f"{scenario_id}: template {key} needs values")
        _require(
            set(values) == set(settings),
            f"{scenario_id}: template {key} must cover exactly the case settings",
        )
        for setting_id, value in values.items():
            setting = settings[setting_id]
            if value is None:
                _require(
                    setting["unknown"] is True,
                    f"{scenario_id}: template {key} leaves known setting {setting_id} empty",
                )
            else:
                _require(
                    value in setting["allowed_values"],
                    f"{scenario_id}: template {key} value for {setting_id} is outside its catalog",
                )
        _require(
            isinstance(template.get("target_proposal"), dict),
            f"{scenario_id}: template {key} needs a target proposal",
        )
        _require(
            isinstance(template.get("reference_results"), list),
            f"{scenario_id}: template {key} needs reference results",
        )


def _validate_rules(
    scenario_id: str,
    rules: JsonObject,
    settings: dict[str, JsonObject],
    sources: dict[str, JsonObject],
) -> None:
    _require(isinstance(rules.get("symptom_rules"), list), f"{scenario_id}: symptom_rules missing")
    for rule in rules["symptom_rules"]:
        for key in ("id", "symptoms", "target_setting_id", "hypothesis", "retest"):
            _require(key in rule, f"{scenario_id}: rule {rule.get('id')} is missing {key}")
        _require(
            rule["target_setting_id"] in settings,
            f"{scenario_id}: rule {rule['id']} targets an unknown setting",
        )
        for key in ("alternative_setting_id",):
            if rule.get(key):
                _require(
                    rule[key] in settings,
                    f"{scenario_id}: rule {rule['id']} references an unknown alternative",
                )
        for key in ("conflicting_protected_setting_ids",):
            for setting_id in rule.get(key, []):
                _require(
                    setting_id in settings,
                    f"{scenario_id}: rule {rule['id']} references unknown protected setting",
                )
        for source_id in rule.get("evidence_source_ids", []):
            _require(
                source_id in sources,
                f"{scenario_id}: rule {rule['id']} references unknown evidence",
            )
    for setting_id in rules.get("protected_options", []):
        _require(
            setting_id in settings,
            f"{scenario_id}: protected option {setting_id} is not in the setting catalog",
        )


def _validate_recipes(scenario_id: str, recipes: JsonObject) -> None:
    for label in RECIPE_LABELS:
        _require(label in recipes, f"{scenario_id}: trace recipe {label} is missing")
        recipe = recipes[label]
        for key in ("frames", "base_interval_us", "jitter_us", "spike_every", "spike_us"):
            _require(
                _is_int(recipe.get(key)) and recipe[key] > 0,
                f"{scenario_id}: recipe {label} needs a positive integer {key}",
            )
        _require(
            recipe["spike_us"] > recipe["base_interval_us"],
            f"{scenario_id}: recipe {label} spike must exceed the base interval",
        )
        _require(
            recipe["frames"] > 1,
            f"{scenario_id}: recipe {label} needs at least two frames",
        )


class ScenarioCatalog:
    """Immutable, validated view of the two demo scenario packs."""

    def __init__(self, root: Path, scenarios: dict[str, JsonObject], failure_scripts: list[JsonObject]):
        self.root = Path(root)
        self._scenarios = scenarios
        self._failure_scripts = failure_scripts

    @classmethod
    def load(cls, root: Path) -> "ScenarioCatalog":
        root = Path(root)
        manifest = _read_json(root / "manifest.json")
        _require(manifest.get("schema") == SCHEMA, "manifest schema mismatch")
        _require(manifest.get("demo_only") is True, "manifest demo_only must be true")
        entries = manifest.get("scenarios")
        _require(isinstance(entries, list), "manifest scenarios must be a list")
        listed = {entry.get("scenario_id") for entry in entries if isinstance(entry, dict)}
        _require(listed == set(SCENARIO_IDS), "manifest must list exactly the two demo cases")

        scenarios: dict[str, JsonObject] = {}
        for entry in entries:
            scenario_id = entry["scenario_id"]
            files = entry.get("files")
            _require(isinstance(files, dict), f"{scenario_id}: manifest files must be a mapping")
            _require(
                set(files) == set(MEMBERS),
                f"{scenario_id}: manifest must list exactly the six fixture members",
            )
            case_dir = root / scenario_id
            payloads: dict[str, JsonObject] = {}
            for member in MEMBERS:
                relative = files[member]
                _require(isinstance(relative, str), f"{scenario_id}: member path must be a string")
                member_path = (root / relative).resolve()
                try:
                    member_path.relative_to(case_dir.resolve())
                except ValueError as exc:
                    raise ScenarioError(
                        f"{scenario_id}: fixture member escapes the case directory"
                    ) from exc
                _require(member_path.is_file(), f"{scenario_id}: missing fixture member {member}")
                digest = hashlib.sha256(member_path.read_bytes()).hexdigest()
                _require(
                    digest == entry.get("hashes", {}).get(member),
                    f"{scenario_id}: fixture member hash mismatch for {member}",
                )
                payloads[member] = _read_json(member_path)
                _check_demo_only(payloads[member], scenario_id)

            context = payloads["context.json"]
            _validate_context(scenario_id, context)
            sources_doc = payloads["sources.json"]
            sources_list = sources_doc.get("sources")
            _require(isinstance(sources_list, list), f"{scenario_id}: sources must be a list")
            pack_version = sources_doc.get("pack_version")
            sources = _validate_sources(scenario_id, sources_list, pack_version=pack_version)
            settings_doc = payloads["settings.json"]
            settings_list = settings_doc.get("settings")
            _require(isinstance(settings_list, list), f"{scenario_id}: settings must be a list")
            settings = _validate_settings(scenario_id, settings_list, sources)
            if pack_version == "mw1-richness/2":
                for setting in settings_list:
                    _require(not setting["unknown"] and len(setting["evidence_source_ids"]) >= 2, f"{setting['id']}: incomplete fixed setting")
                    decision = setting.get("evidence_decision") or {}
                    _require(len(decision.get("source_views", [])) >= 2 and bool(decision.get("decision")) and bool(decision.get("tradeoff")), f"{setting['id']}: missing decision evidence")
                    allowed_keys = {sources[source_id].get("source_key") for source_id in setting["evidence_source_ids"]}
                    _require(all(view.get("source_key") in allowed_keys and bool(view.get("view")) for view in decision["source_views"]), f"{setting['id']}: decision references missing source")
            templates_doc = payloads["templates.json"]
            templates = templates_doc.get("templates")
            _require(isinstance(templates, dict), f"{scenario_id}: templates must be an object")
            _validate_templates(scenario_id, templates, settings)
            rules_doc = payloads["rules.json"]
            _validate_rules(scenario_id, rules_doc, settings, sources)
            recipes_doc = payloads["trace-recipes.json"]
            recipes = recipes_doc.get("recipes")
            _require(isinstance(recipes, dict), f"{scenario_id}: recipes must be an object")
            _validate_recipes(scenario_id, recipes)

            scenarios[scenario_id] = {
                "scenario_id": scenario_id,
                "title": context["game_name"],
                "gpu_name": context["gpu_name"],
                "context": context,
                "settings": settings_list,
                "sources": sources_list,
                "source_pack_version": pack_version,
                "templates": templates,
                "rules": rules_doc,
                "trace_recipes": recipes,
                "_settings_by_id": settings,
                "_sources_by_id": sources,
            }

        all_settings: set[str] = set()
        all_sources: set[str] = set()
        for scenario_id, scenario in scenarios.items():
            ids = {setting["id"] for setting in scenario["settings"]}
            _require(not (ids & all_settings), f"{scenario_id}: setting ids leak across cases")
            all_settings |= ids
            source_ids = {source["id"] for source in scenario["sources"]}
            _require(
                not (source_ids & all_sources), f"{scenario_id}: source ids leak across cases"
            )
            all_sources |= source_ids

        failure_doc = _read_json(root / "failure-scripts.json")
        _require(failure_doc.get("schema") == SCHEMA, "failure script schema mismatch")
        _require(failure_doc.get("demo_only") is True, "failure scripts must be demo only")
        scripts = failure_doc.get("scripts")
        _require(isinstance(scripts, list), "failure scripts must be a list")
        return cls(root, scenarios, scripts)

    def get(self, scenario_id: str) -> JsonObject:
        try:
            return self._scenarios[scenario_id]
        except KeyError as exc:
            raise ScenarioError(f"unknown demo scenario: {scenario_id}") from exc

    def list(self) -> list[JsonObject]:
        return [self._scenarios[key] for key in ACTIVE_SCENARIO_IDS]

    def failure_scripts(self) -> list[JsonObject]:
        return list(self._failure_scripts)

    def source(self, scenario_id: str, source_id: str) -> JsonObject:
        try:
            return self.get(scenario_id)["_sources_by_id"][source_id]
        except KeyError as exc:
            raise ScenarioError(f"{scenario_id}: unknown demo source {source_id}") from exc

    def setting(self, scenario_id: str, setting_id: str) -> JsonObject:
        try:
            return self.get(scenario_id)["_settings_by_id"][setting_id]
        except KeyError as exc:
            raise ScenarioError(f"{scenario_id}: unknown demo setting {setting_id}") from exc

    def recipe(self, scenario_id: str, label: str) -> JsonObject:
        try:
            return self.get(scenario_id)["trace_recipes"][label]
        except KeyError as exc:
            raise ScenarioError(f"{scenario_id}: unknown trace recipe {label}") from exc
