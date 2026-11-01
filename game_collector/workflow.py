"""Bounded single-process discovery, extraction, follow-up and export."""

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import time
from uuid import uuid4

from pydantic import ValidationError

from .providers import ProviderError
from .json_format import repair_format
from .extraction_parse import parse_extraction
from .reconciliation import evidence_signature, recheck_configurations, resolution_is_grounded
from .reader import canonical_url
from .schema import Extraction, now
from .storage import Store
from .validation import validate_extraction


@dataclass
class Limits:
    max_urls: int = 12
    max_searches: int = 12
    max_followup_rounds: int = 2
    max_queries_per_gap: int = 2
    max_model_calls: int = 16
    max_read_calls: int = 36
    max_seconds: float | None = None

    def __post_init__(self):
        for key, value in asdict(self).items():
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError(f"invalid budget: {key}")


class BudgetExceeded(RuntimeError):
    pass


class Collector:
    def __init__(self, search, reader, model, progress=None):
        self.search, self.reader, self.model = search, reader, model
        self.progress = progress or (lambda message: None)

    def run(self, request: dict, output_dir):
        if not request.get("game_name") or request.get("mode", "discover") not in {"discover", "seeded"}:
            raise ValueError("game_name and a valid mode are required")
        request = {"mode": "discover", "platform_scope": "Windows PC", "seed_urls": [], **request}
        if request["mode"] == "seeded" and not request["seed_urls"]:
            raise ValueError("seeded mode needs at least one URL")
        output = Path(output_dir)
        if output.exists() and any(output.iterdir()):
            raise FileExistsError("output directory must be empty; previous runs are never overwritten")
        self.limits = Limits(**request.get("budget", {}))
        self.request, self.sources, self.records, self.gaps, self.extractions = request, [], [], [], []
        self.calls, self.log, self.seen_urls, self.hashes = Counter(), [], set(), {}
        self.started, self.model_disabled = time.monotonic(), False
        self.run_id = uuid4().hex
        self.store = Store(output)
        self.state = {"id": self.run_id, "game_id": hashlib.sha256(request["game_name"].casefold().encode()).hexdigest()[:16], "contract_version": "0.1", "started_at": now(), "input": request, "budget": asdict(self.limits), "tools": {"search": getattr(self.search, "name", "none"), "reader": getattr(self.reader, "name", "none"), "model": getattr(self.model, "name", "disabled")}, "status": "partial_scope", "stop_reason": "source_unavailable", "coverage": {"discovery_exercised": False, "initial_queries_executed": 0, "scope": "selected_public_text_sources_within_budget"}}
        stop = None
        try:
            candidates = [{"url": u, "role": "seed"} for u in request["seed_urls"]]
            if request["mode"] == "discover":
                gpu = request.get("gpu", "")
                for role, query in [
                    ("official_version_compatibility", f"{request['game_name']} PC official graphics settings release notes compatibility"),
                    ("conditional_measurement", f"{request['game_name']} {gpu} PC optimization benchmarks settings test conditions"),
                    ("configuration_tradeoffs", f"{request['game_name']} PC graphics settings guide GPU tiers quality performance"),
                ]:
                    self.state["coverage"]["discovery_exercised"] = True
                    results = self._search(query, role)
                    self.state["coverage"]["initial_queries_executed"] += 1
                    candidates.extend({**r, "role": role} for r in results)
            for candidate in candidates:
                url = canonical_url(candidate["url"])
                if url not in self.seen_urls:
                    self._process(url, candidate.get("role", "discovered"))
            no_gain = 0
            for round_index in range(self.limits.max_followup_rounds):
                reread_urls = set()
                pending = sorted((g for g in self.gaps if g["status"] == "open"), key=lambda g: g["priority"])
                if not pending:
                    break
                before = self._information_signature()
                for gap in pending:
                    if gap["status"] != "open":
                        continue
                    if gap["field"] == "extraction" and (self.model is None or self.model_disabled):
                        continue  # More pages cannot repair missing model access.
                    # First revisit the actual source, not another generic guide.
                    if not any(a["action"] == "reread" for a in gap["attempts"]):
                        source = next((s for s in self.sources if s["id"] in gap["source_ids"]), None)
                        if source and source["status"] in {"read", "extracted"} and source["url"] not in reread_urls:
                            reread_urls.add(source["url"])
                            gap["attempts"].append({"round": round_index + 1, "action": "reread", "url": source["url"], "reason": gap["question"]})
                            self._process(source["url"], "followup_original_source", force=True, focused_gap=gap)
                    if gap["status"] != "open":
                        continue
                    if gap["field"] in {"validation", "schema"}:
                        continue  # Another author cannot repair this author's broken locator.
                    queries = sum(a["action"] == "search" for a in gap["attempts"])
                    if queries < self.limits.max_queries_per_gap:
                        query = gap.get("query") or f"{request['game_name']} {gap['question']}"
                        gap["attempts"].append({"round": round_index + 1, "action": "search", "query": query, "reason": gap["reason"]})
                        for hit in self._search(query, "followup:" + gap["id"]):
                            url = canonical_url(hit["url"])
                            if url not in self.seen_urls:
                                self._process(url, "followup_new_source", focused_gap=gap)
                                if gap["status"] == "resolved":
                                    break
                    self._checkpoint()
                no_gain = no_gain + 1 if self._information_signature() == before else 0
                if no_gain >= 2:
                    stop = "low_information_gain"
                    break
            if stop is None:
                open_gaps = [g for g in self.gaps if g["status"] == "open"]
                stop = "unresolved_conflict" if any(g["priority"] in {"P0", "P1"} for g in open_gaps) else "coverage_reached"
        except BudgetExceeded:
            stop = "budget_exhausted"
        except KeyboardInterrupt:
            stop = "budget_exhausted"
            self.state["interrupted"] = True
        finally:
            stop = stop or "source_unavailable"
            self.state["stop_reason"] = stop
            for gap in self.gaps:
                if gap["status"] == "open":
                    gap.update(status="stopped", stop_reason=stop)
            has_records = any(r["record_status"] == "accepted" for r in self.records)
            critical = any(g["status"] != "resolved" and g["priority"] in {"P0", "P1"} for g in self.gaps)
            self.state["status"] = "completed_scope" if has_records and not critical and stop == "coverage_reached" else "partial_scope"
            self.state["finished_at"] = now()
            self._checkpoint()
            self.store.close()
        return self.state

    def _call(self, kind, detail, function, url=None):
        if self.limits.max_seconds is not None and time.monotonic() - self.started >= self.limits.max_seconds:
            raise BudgetExceeded("time limit")
        caps = {"search": self.limits.max_searches, "model": self.limits.max_model_calls, "read": self.limits.max_read_calls}
        if self.calls[kind] >= caps[kind]:
            raise BudgetExceeded(kind + " limit")
        if url and url not in self.seen_urls:
            if len(self.seen_urls) >= self.limits.max_urls:
                raise BudgetExceeded("URL limit")
            self.seen_urls.add(url)
        self.calls[kind] += 1
        event = {"kind": kind, "detail": detail, "started_at": now(), "status": "running"}
        self.log.append(event)
        self.progress(f"{kind}: {detail}")
        started = time.monotonic()
        try:
            result = function()
            event["status"] = "ok"
            return result
        except ProviderError as error:
            event.update(status="failed", error=str(error))
            raise
        finally:
            event["elapsed_seconds"] = round(time.monotonic() - started, 3)

    def _search(self, query, role):
        try:
            return self._call("search", query, lambda: self.search.search(query, 4))
        except ProviderError as error:
            self._gap("search:" + role, "P1", "search", "Search failed: " + str(error), query)
            return []

    def _gap(self, key, priority, field, reason, question, source_id=None, record_ids=None, query=None):
        gap_id = hashlib.sha256(key.encode()).hexdigest()[:16]
        gap = next((g for g in self.gaps if g["id"] == gap_id), None)
        if gap is None:
            gap = {"id": gap_id, "priority": priority, "field": field, "reason": reason, "question": question, "query": query, "source_ids": [source_id] if source_id else [], "record_ids": record_ids or [], "status": "open", "resolution_evidence_ids": [], "attempts": [], "stop_reason": None}
            self.gaps.append(gap)
        return gap

    def _process(self, url, role, force=False, focused_gap=None):
        source = {"id": uuid4().hex, "revision_id": uuid4().hex, "url": url, "retrieved_at": now(), "role": role, "status": "failed", "original_source_url": None, "lineage_status": "unknown"}
        doc, last_error = None, None
        for attempt in range(2):
            try:
                doc = self._call("read", url, lambda: self.reader.read(url), url=url)
                break
            except ProviderError as error:
                last_error = str(error)
                if not error.retryable:
                    break
        if doc is None and hasattr(self.reader, "read_alternative"):
            try:
                doc = self._call("read", "alternative:" + url, lambda: self.reader.read_alternative(url), url=url)
            except ProviderError as error:
                last_error = str(error)
        if doc is None:
            source["error"] = last_error
            self.sources.append(source)
            self._gap(url + ":read", "P1", "source", last_error or "source unavailable", "Can a public readable source answer this URL's intended role?", source["id"])
            self._checkpoint()
            return
        source.update(status="read", revision_id=doc.revision_id, document=doc.model_dump(), content_hash=doc.content_hash)
        previous = self.hashes.get(doc.content_hash)
        if previous:
            source["duplicate_of"] = previous
            source["lineage_status"] = "identical_captured_content"
        else:
            self.hashes[doc.content_hash] = source["id"]
        self.sources.append(source)
        body = " ".join(b.text for b in doc.blocks)
        wheel_title = re.search(r"fanatec|steering|racing.wheel|force.feedback|方向盘", doc.title + " " + url, re.I)
        graphics_body = re.search(r"\bDLSS\b|\bFSR\b|ray.tracing|graphics.settings|texture.quality|shadow.quality|画质|光追", body, re.I)
        if wheel_title and not graphics_body:
            source.update(status="out_of_scope", relevance_reason="wheel/controller guide without identified graphics coverage")
            self._gap(url + ":topic", "P1", "coverage", "Source covers wheel/controller settings", "Find PC graphics and display settings, excluding wheel/force-feedback presets.", source["id"], query=self.request["game_name"] + " PC graphics settings DLSS textures shadows")
            self._checkpoint()
            return
        if previous and not force:
            self._checkpoint()
            return
        if doc.truncated:
            self._gap(url + ":truncated", "P1", "capture_scope", "Document exceeded input window", "Which omitted sections contain necessary settings or conditions?", source["id"])
        if self.model is None or self.model_disabled:
            self._gap("model_unavailable", "P0", "extraction", "Model disabled or unavailable", "Configure a working model and extract the saved public sources.", source["id"])
            self._checkpoint()
            return
        context = {k: v for k, v in self.request.items() if k not in {"budget", "seed_urls"}}
        context["pending_gaps"] = [g for g in self.gaps if g["status"] == "open"][:20]
        if focused_gap:
            context["focused_gap_id"] = focused_gap["id"]
        self._checkpoint()  # Save the newly captured source before a slow external model call.
        try:
            result = self._call("model", source["id"], lambda: self.model.extract(doc, context))
        except ProviderError as error:
            source["extraction_error"] = str(error)
            if any(code in str(error) for code in ["401", "402", "403"]):
                self.model_disabled = True
            self._gap(url + ":model", "P0", "extraction", str(error), "Restore model access and extract this saved source.", source["id"])
            self._checkpoint()
            return
        output = {"source_id": source["id"], **result}
        self.extractions.append(output)
        if result.get("incomplete_reason"):
            self._gap(url + ":incomplete", "P0", "extraction", "Model response incomplete: " + result["incomplete_reason"], "Re-extract the saved source without truncation.", source["id"])
            self._checkpoint()
            return
        raw = result.get("raw", "")
        data = result.get("data")
        if data is None:
            data, repair = repair_format(raw)
            if repair:
                output["format_repair"] = repair
        extraction, invalid_rows, schema_errors = parse_extraction(data)
        if schema_errors:
            output["schema_errors"] = schema_errors
        if extraction is None:
            # Never ask a model to silently rewrite factual fields as a "format repair".
            self._gap(url + ":schema", "P0", "schema", "Invalid extraction shape; raw output retained", "Re-read source to repair extraction without inventing values.", source["id"])
            self._checkpoint()
            return
        output["parsed"] = extraction.model_dump()
        source["metadata"] = extraction.metadata.model_dump()
        source["status"] = "extracted"
        prior_signatures = {evidence_signature(r) for r in self.records if r["record_type"] == "evidence" and r["record_status"] == "accepted"}
        rows = validate_extraction(extraction, doc, context) + invalid_rows
        prefix = source["id"] + ":"
        for row in rows:
            row.update(id=prefix + row["local_id"], source_id=source["id"], revision_id=source["revision_id"], game_id=self.state["game_id"], reference_scope=source["id"])
            self.records.append(row)
            if row["record_status"] == "quarantined":
                self._gap(row["id"] + ":validation", "P0", "validation", ", ".join(row["issues"]), "Repair this record using its exact source position: " + row["local_id"], source["id"], [row["id"]])
        if not rows:
            self._gap(url + ":no_records", "P1", "coverage", "No settings or evidence were extracted", "Find a readable source containing the required graphics knowledge.", source["id"])
        for gap in extraction.gaps:
            self._gap(url + ":" + gap.field + ":" + gap.question, gap.priority, gap.field, gap.reason, gap.question, source["id"], [prefix + i for i in gap.record_ids], gap.query)
        accepted_evidence = {r["local_id"]: r for r in rows if r["record_type"] == "evidence" and r["record_status"] == "accepted"}
        recheck_configurations(self.records, context)
        # A corrected revision may supersede a quarantined record, never erase it.
        for gap in self.gaps:
            if gap["status"] != "open" or gap["field"] != "validation":
                continue
            for old in self.records:
                if old["id"] not in gap["record_ids"] or old["source_id"] == source["id"]:
                    continue
                old_source = next((s for s in self.sources if s["id"] == old["source_id"]), None)
                corrected = next((r for r in rows if r["local_id"] == old["local_id"] and r["record_status"] == "accepted"), None)
                if corrected and old_source and old_source["url"] == url:
                    old["superseded_by"] = corrected["id"]
                    gap.update(status="resolved", resolution_evidence_ids=[corrected["id"]], resolution_method="source_reread_and_record_revalidated")
        for resolution in extraction.resolutions:
            gap = next((g for g in self.gaps if g["id"] == resolution.gap_id and g["status"] == "open"), None)
            if gap and resolution_is_grounded(resolution, gap, accepted_evidence, prior_signatures, self.records):
                # The model's answer is traceable, not independently proved. No automatic config rewrite.
                gap.update(status="resolved", resolution_evidence_ids=[prefix + e for e in resolution.evidence_ids], resolution_explanation=resolution.explanation, resolution_method="model_answer_with_accepted_source_evidence")
        self._checkpoint()

    def _information_signature(self):
        resolved = sum(g["status"] == "resolved" for g in self.gaps)
        # Revisions/reposts do not count as fresh independent information.
        accepted = {(r["record_type"], json.dumps(r["payload"], sort_keys=True, ensure_ascii=False)) for r in self.records if r["record_status"] == "accepted"}
        return resolved, len(accepted)

    def _checkpoint(self):
        self.state["calls"] = dict(self.calls)
        self.state["call_log"] = self.log
        self.state["distinct_urls"] = len(self.seen_urls)
        self.state["actual_models"] = sorted({x.get("model", "unknown") for x in self.extractions})
        self.store.save(self.state, self.sources, self.records, self.gaps, self.extractions)
