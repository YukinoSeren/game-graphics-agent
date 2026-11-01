"""One conservative syntax repair; never replace scalar text or invent values."""

import json
import re


def repair_format(raw):
    fenced = re.fullmatch(r"\s*```(?:json)?\s*\n(.*?)\n```\s*", raw, re.S)
    if fenced:
        try:
            return json.loads(fenced[1]), {"method": "strip_json_fence_only", "before": raw, "after": fenced[1], "facts_changed": False}
        except json.JSONDecodeError:
            return None, None  # At most one format repair, not a repair chain.
    stack, out, edits = [], [], []
    quoted = escaped = False
    for pos, char in enumerate(raw):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            stack.append(char)
        elif char in "]}":
            expected = "[" if char == "]" else "{"
            if stack and stack[-1] != expected:
                missing = "]" if stack.pop() == "[" else "}"
                edits.append({"offset": pos, "insert": missing})
                out.append(missing)
            if not stack or stack[-1] != expected:
                return None, None
            stack.pop()
        out.append(char)
    if quoted:
        return None, None
    if len(stack) == 1:
        missing = "]" if stack.pop() == "[" else "}"
        edits.append({"offset": len(raw), "insert": missing})
        out.append(missing)
    if stack or len(edits) != 1:
        return None, None
    repaired = "".join(out)
    try:
        data = json.loads(repaired)
    except json.JSONDecodeError:
        return None, None
    return data, {"method": "insert_single_missing_closer", "before": raw, "after": repaired, "edits": edits, "facts_changed": False}
