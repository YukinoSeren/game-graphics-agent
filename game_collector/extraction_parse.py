"""Isolate malformed records without rewriting their facts or dropping valid peers."""

from pydantic import TypeAdapter, ValidationError

from .schema import Extraction, RECORD_GROUPS


def parse_extraction(data):
    try:
        return Extraction.model_validate(data), [], []
    except ValidationError as error:
        errors = [{"loc": list(e["loc"]), "type": e["type"]} for e in error.errors()]
    if not isinstance(data, dict):
        return None, [], errors
    groups = (*RECORD_GROUPS, "gaps", "resolutions")
    header = {**data, **{group: [] for group in groups}}
    try:
        parsed = Extraction.model_validate(header)
    except ValidationError:
        return None, [], errors  # Identity/provenance header cannot be safely salvaged.
    invalid = []
    for group in groups:
        values = data.get(group, [])
        if not isinstance(values, list):
            invalid.append({"local_id": "invalid:" + group, "record_type": group, "payload": values, "record_status": "quarantined", "issues": ["schema_list_required"]})
            continue
        good = []
        for index, value in enumerate(values):
            try:
                good.extend(TypeAdapter(Extraction.model_fields[group].annotation).validate_python([value]))
            except ValidationError:
                invalid.append({"local_id": str(value.get("id", f"invalid:{group}:{index}")) if isinstance(value, dict) else f"invalid:{group}:{index}", "record_type": group, "payload": value, "record_status": "quarantined", "issues": ["schema_record_invalid"], "extraction_status": "failed", "applicability": "unknown", "recommendation_status": "needs_followup" if group == "configurations" else None})
        setattr(parsed, group, good)
    return parsed, invalid, errors
