#!/usr/bin/env python3
"""Transaction-safe merge of sub-agent meta observations into glossary.

Sub-agents emit `output_chunk<NNNN>.meta.json` alongside their translated
chunk. This script merges those observations into the canonical
`glossary.json` deterministically and transactionally.

Why transactional: glossary is the source of truth for terminology across
the whole book. If a merge partially succeeds (some decisions applied,
others rejected), the glossary is left in an inconsistent state and the
next batch of sub-agents sees a mix of old/new term mappings.

Usage:
    python3 merge_meta.py prepare-merge <temp_dir>
        Reads all unconsumed `output_chunk*.meta.json` files and prints
        JSON with:
          - auto_apply: list of items that can be applied without
            human/agent decision (no collision, unanimous across chunks)
          - decisions_needed: list of items requiring a choice (each has
            `id`, `kind`, `options`)
          - consumed_chunk_ids: chunk_ids whose meta was scanned (will be
            recorded in applied_meta_hashes on successful apply-merge)
          - malformed_meta_chunk_ids: meta files that failed validation
            (quarantined — not consumed, not crashing the run)

    echo '<JSON>' | python3 merge_meta.py apply-merge <temp_dir>
        Atomic apply. Reads JSON from stdin with shape:
          { "auto_apply": [...], "decisions": [...],
            "consumed_chunk_ids": [...] }
        If ANY decision is malformed (wrong choice for kind, missing
        fields, references non-existent entity), the entire batch aborts
        with non-zero exit and NO glossary mutation, NO hash recording.
        On success: glossary.json is updated (via .tmp + rename),
        consumed meta hashes are recorded in applied_meta_hashes.

    python3 merge_meta.py status <temp_dir>
        Observability snapshot: counts of meta files found / consumed /
        malformed, and unmerged (sub-agent-compliance) chunks.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (cp1251 default breaks on non-ASCII)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))


def process_dir(temp_dir: Path) -> Path:
    """Return process subdirectory (creates if needed).

    Layout:
      <temp_dir>/             - human-facing (glossary.json, voice book, book.*, reports)
      <temp_dir>/process/     - machine-facing (chunks, metas, manifests, configs)
    """
    p = temp_dir / "process"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def stable_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def load_glossary(temp_dir: Path) -> dict:
    path = temp_dir / "glossary.json"
    if not path.exists():
        return {
            "version": 2,
            "terms": [],
            "high_frequency_top_n": 20,
            "applied_meta_hashes": {},
        }
    # Use read_json_safe to handle encoding issues on Windows (surrogates,
    # mixed encodings from editors). Sanitizes data for clean serialization.
    from config import read_json_safe

    try:
        g = read_json_safe(path)
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARN: could not parse glossary.json: {e}", file=sys.stderr)
        return {
            "version": 2,
            "terms": [],
            "high_frequency_top_n": 20,
            "applied_meta_hashes": {},
        }
    if g.get("version") != 2:
        print(
            f"WARN: glossary version != 2 (got {g.get('version')}); treating as v2 with existing terms.",
            file=sys.stderr,
        )
    g.setdefault("terms", [])
    g.setdefault("high_frequency_top_n", 20)
    g.setdefault("applied_meta_hashes", {})
    return g


def save_glossary_atomic(temp_dir: Path, glossary: dict):
    """Windows-safe atomic write of glossary.json.

    Uses atomic_write_json from config module — retries on PermissionError
    (common on Windows when file is locked by antivirus/editor) and falls
    back to non-atomic write if needed. See config.atomic_write_json.
    """
    from config import atomic_write_json

    atomic_write_json(temp_dir / "glossary.json", glossary, indent=2, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────
# Meta file validation
# ─────────────────────────────────────────────────────────────────────

ASCII_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z\s'\-]*$")


def is_valid_entity_source(source: str) -> bool:
    """Entity source must be ASCII letters/spaces/apostrophes/hyphens.

    Non-ASCII sources in new_entities are hallucinations — sub-agent
    proposing a Russian name as source is a bug.
    """
    if not source or not isinstance(source, str):
        return False
    return bool(ASCII_NAME_RE.match(source))


def validate_meta(meta: dict) -> tuple[bool, list[str]]:
    """Validate a meta dict. Returns (is_valid, list_of_errors)."""
    errors = []
    if not isinstance(meta, dict):
        return False, ["meta is not a dict"]

    if "chunk_id" in meta:
        errors.append("'chunk_id' field present — must be absent (derived from filename)")

    # new_entities
    for i, e in enumerate(meta.get("new_entities", [])):
        if not isinstance(e, dict):
            errors.append(f"new_entities[{i}] not a dict")
            continue
        src = e.get("source", "")
        if not is_valid_entity_source(src):
            errors.append(f"new_entities[{i}].source invalid: {src!r}")
        if not e.get("target_proposal"):
            errors.append(f"new_entities[{i}].target_proposal empty")

    # alias_hypotheses
    for i, a in enumerate(meta.get("alias_hypotheses", [])):
        if not isinstance(a, dict):
            errors.append(f"alias_hypotheses[{i}] not a dict")
            continue
        if not a.get("variant") or not a.get("may_be_alias_of_source"):
            errors.append(f"alias_hypotheses[{i}] missing variant or may_be_alias_of_source")

    # attribute_hypotheses
    for i, ah in enumerate(meta.get("attribute_hypotheses", [])):
        if not isinstance(ah, dict):
            errors.append(f"attribute_hypotheses[{i}] not a dict")
            continue
        if not ah.get("entity_source") or not ah.get("attribute") or not ah.get("value"):
            errors.append(f"attribute_hypotheses[{i}] missing required fields")

    # conflicts
    for i, c in enumerate(meta.get("conflicts", [])):
        if not isinstance(c, dict):
            errors.append(f"conflicts[{i}] not a dict")
            continue
        if not c.get("entity_source") or not c.get("field"):
            errors.append(f"conflicts[{i}] missing entity_source or field")

    return (len(errors) == 0), errors


# ─────────────────────────────────────────────────────────────────────
# prepare-merge
# ─────────────────────────────────────────────────────────────────────


def find_existing_term(glossary: dict, source: str) -> dict | None:
    """Find a term by source (case-insensitive) or by alias."""
    src_lower = source.lower()
    for t in glossary["terms"]:
        if t["source"].lower() == src_lower:
            return t
        for alias in t.get("aliases", []):
            if alias.lower() == src_lower:
                return t
    return None


def prepare_merge(temp_dir: Path) -> dict:
    """Scan all unconsumed meta files and produce a merge plan."""
    glossary = load_glossary(temp_dir)
    applied_hashes = glossary.get("applied_meta_hashes", {})

    # Gather all meta files
    auto_apply: list[dict] = []
    decisions_needed: list[dict] = []
    consumed_chunk_ids: list[str] = []
    malformed_chunk_ids: list[str] = []

    # Aggregated observations across all unconsumed metas
    # Key: (kind, identifier) -> list of (chunk_id, payload, evidence)
    new_entity_proposals: dict[str, list] = {}  # source -> proposals
    alias_proposals: dict[tuple, list] = {}  # (variant, candidate) -> proposals
    attribute_proposals: dict[tuple, list] = {}  # (entity, attribute, value) -> proposals
    conflict_reports: dict[tuple, list] = {}  # (entity, field, injected) -> reports

    from config import read_text_safe

    for mp in sorted(process_dir(temp_dir).glob("output_chunk*.meta.json")):
        # Derive chunk_id from filename: output_chunk0042.meta.json -> chunk0042
        cid = mp.name.replace("output_", "").replace(".meta.json", "")

        # Read with errors='replace' to handle encoding issues on Windows.
        # Sub-agents may write meta files with bytes that aren't valid UTF-8
        # (e.g., cp1251 console output mixed in). Replace bad bytes with U+FFFD.
        meta_text = read_text_safe(mp)
        meta_hash = stable_hash(meta_text)
        if applied_hashes.get(cid) == meta_hash:
            continue  # already consumed

        try:
            meta = json.loads(meta_text)
        except json.JSONDecodeError as e:
            malformed_chunk_ids.append(cid)
            print(f"WARN: malformed JSON in {mp.name}: {e}", file=sys.stderr)
            continue

        is_valid, errors = validate_meta(meta)
        if not is_valid:
            malformed_chunk_ids.append(cid)
            for err in errors:
                print(f"WARN: {mp.name}: {err}", file=sys.stderr)
            continue

        consumed_chunk_ids.append(cid)

        # Aggregate new_entities
        for e in meta.get("new_entities", []):
            src = e["source"]
            new_entity_proposals.setdefault(src, []).append(
                {
                    "chunk_id": cid,
                    "target_proposal": e.get("target_proposal", ""),
                    "category": e.get("category", "other"),
                    "evidence": e.get("evidence", ""),
                }
            )

        # Aggregate alias_hypotheses
        for a in meta.get("alias_hypotheses", []):
            key = (a["variant"], a["may_be_alias_of_source"])
            alias_proposals.setdefault(key, []).append(
                {
                    "chunk_id": cid,
                    "evidence": a.get("evidence", ""),
                }
            )

        # Aggregate attribute_hypotheses
        for ah in meta.get("attribute_hypotheses", []):
            key = (ah["entity_source"], ah["attribute"], ah["value"])
            attribute_proposals.setdefault(key, []).append(
                {
                    "chunk_id": cid,
                    "confidence": ah.get("confidence", "medium"),
                    "evidence": ah.get("evidence", ""),
                }
            )

        # Aggregate conflicts
        for c in meta.get("conflicts", []):
            key = (c["entity_source"], c["field"], c.get("injected", ""))
            conflict_reports.setdefault(key, []).append(
                {
                    "chunk_id": cid,
                    "observed_better": c.get("observed_better", ""),
                    "evidence": c.get("evidence", ""),
                }
            )

    # ── Resolve new_entities ────────────────────────────────────────
    for src, proposals in new_entity_proposals.items():
        existing = find_existing_term(glossary, src)
        if existing:
            # Source already in glossary — promote to conflict if target differs
            targets = {p["target_proposal"] for p in proposals}
            if existing["target"] in targets and len(targets) == 1:
                # Unanimous match with existing — no action needed
                continue
            # Conflict between existing and proposals
            decisions_needed.append(
                {
                    "id": f"new_entity_conflict_{src}",
                    "kind": "existing_entity_conflict",
                    "entity_source": src,
                    "current_target": existing["target"],
                    "current_category": existing.get("category", ""),
                    "proposed_variants": [
                        {
                            "target_proposal": p["target_proposal"],
                            "category": p["category"],
                            "evidence": p["evidence"],
                            "evidence_chunks": [p["chunk_id"]],
                        }
                        for p in proposals
                    ],
                    "options": ["keep_current"]
                    + [f"use_variant_{i}" for i in range(len(proposals))]
                    + ["record_in_notes"],
                }
            )
            continue

        # New entity, not in glossary
        targets = {p["target_proposal"] for p in proposals}
        categories = {p["category"] for p in proposals}
        if len(targets) == 1 and len(categories) == 1:
            # Unanimous — auto-apply
            auto_apply.append(
                {
                    "action": "add_entity",
                    "source": src,
                    "target": next(iter(targets)),
                    "category": next(iter(categories)),
                    "evidence_refs": [p["chunk_id"] for p in proposals],
                }
            )
        else:
            # Conflicting proposals for new entity
            decisions_needed.append(
                {
                    "id": f"conflicting_new_entity_{src}",
                    "kind": "conflicting_new_entity_proposals",
                    "source": src,
                    "variants": [
                        {
                            "target_proposal": p["target_proposal"],
                            "category": p["category"],
                            "evidence": p["evidence"],
                            "evidence_chunks": [p["chunk_id"]],
                        }
                        for p in proposals
                    ],
                    "options": [f"use_variant_{i}" for i in range(len(proposals))] + ["skip"],
                }
            )

    # ── Resolve alias_hypotheses ────────────────────────────────────
    for (variant, candidate), proposals in alias_proposals.items():
        candidate_term = find_existing_term(glossary, candidate)
        if not candidate_term:
            # Candidate doesn't exist — defer (maybe a new_entity will create it)
            decisions_needed.append(
                {
                    "id": f"alias_{variant}_{candidate}",
                    "kind": "alias",
                    "variant": variant,
                    "candidate_source": candidate,
                    "evidence": proposals[0]["evidence"],
                    "evidence_chunks": [p["chunk_id"] for p in proposals],
                    "options": ["yes_alias", "no_separate_entity", "skip"],
                }
            )
            continue
        # Check if variant already aliased to candidate
        if variant.lower() in [a.lower() for a in candidate_term.get("aliases", [])]:
            continue  # already aliased
        # Check if variant is a separate entity
        if find_existing_term(glossary, variant):
            # variant is its own entity — can't also be alias
            decisions_needed.append(
                {
                    "id": f"alias_{variant}_{candidate}",
                    "kind": "alias",
                    "variant": variant,
                    "candidate_source": candidate,
                    "evidence": proposals[0]["evidence"],
                    "evidence_chunks": [p["chunk_id"] for p in proposals],
                    "options": ["yes_alias", "no_separate_entity", "skip"],
                }
            )
            continue
        # Auto-apply: variant not in glossary, candidate exists, no conflict
        auto_apply.append(
            {
                "action": "add_alias",
                "variant": variant,
                "to_source": candidate,
            }
        )

    # ── Resolve attribute_hypotheses ────────────────────────────────
    for (entity, attr, value), proposals in attribute_proposals.items():
        term = find_existing_term(glossary, entity)
        if not term:
            continue  # entity doesn't exist yet — skip; will re-scan next batch
        current_value = term.get(attr)
        if current_value == value:
            continue  # already set
        if not current_value or current_value == "unknown":
            # Auto-apply if all proposing chunks agree
            if len(proposals) >= 1:
                max_confidence = max({"high": 3, "medium": 2, "low": 1}.get(p["confidence"], 1) for p in proposals)
                if max_confidence >= 2:  # medium or high
                    auto_apply.append(
                        {
                            "action": "set_attribute",
                            "entity_source": entity,
                            "attribute": attr,
                            "value": value,
                            "evidence_refs": [p["chunk_id"] for p in proposals],
                        }
                    )
                else:
                    decisions_needed.append(
                        {
                            "id": f"attr_{entity}_{attr}",
                            "kind": "attribute_low_confidence",
                            "entity_source": entity,
                            "attribute": attr,
                            "proposed_value": value,
                            "evidence": proposals[0]["evidence"],
                            "evidence_chunks": [p["chunk_id"] for p in proposals],
                            "options": ["accept", "skip"],
                        }
                    )
        else:
            # Conflict: existing value differs from proposal
            decisions_needed.append(
                {
                    "id": f"attr_conflict_{entity}_{attr}",
                    "kind": "attribute_conflict",
                    "entity_source": entity,
                    "attribute": attr,
                    "current_value": current_value,
                    "proposed_value": value,
                    "evidence": proposals[0]["evidence"],
                    "evidence_chunks": [p["chunk_id"] for p in proposals],
                    "options": ["keep_current", "accept_proposed", "record_in_notes"],
                }
            )

    # ── Resolve conflicts ───────────────────────────────────────────
    for (entity, field, injected), reports in conflict_reports.items():
        decisions_needed.append(
            {
                "id": f"conflict_{entity}_{field}",
                "kind": "conflict",
                "entity_source": entity,
                "field": field,
                "current": injected,
                "proposed": reports[0]["observed_better"],
                "evidence": reports[0]["evidence"],
                "evidence_chunks": [r["chunk_id"] for r in reports],
                "options": ["keep_current", "accept_proposed", "record_in_notes"],
            }
        )

    return {
        "auto_apply": auto_apply,
        "decisions_needed": decisions_needed,
        "consumed_chunk_ids": consumed_chunk_ids,
        "malformed_meta_chunk_ids": malformed_chunk_ids,
    }


# ─────────────────────────────────────────────────────────────────────
# apply-merge (transactional)
# ─────────────────────────────────────────────────────────────────────

VALID_CHOICE_FOR_KIND = {
    "alias": {"yes_alias", "no_separate_entity", "skip"},
    "conflict": {"keep_current", "accept_proposed", "record_in_notes"},
    "existing_entity_conflict": None,  # dynamic — has keep_current + use_variant_N + record_in_notes
    "conflicting_new_entity_proposals": None,  # dynamic — use_variant_N + skip
    "attribute_low_confidence": {"accept", "skip"},
    "attribute_conflict": {"keep_current", "accept_proposed", "record_in_notes"},
}


def validate_decisions(decisions: list, prepared: dict) -> list[str]:
    """Validate each decision against its kind. Returns list of errors."""
    errors = []

    # Build a lookup of prepared decisions
    prepared_by_id = {d["id"]: d for d in prepared.get("decisions_needed", [])}

    for d in decisions:
        did = d.get("id")
        if did not in prepared_by_id:
            errors.append(f"decision {did!r} not in prepared decisions_needed")
            continue
        p = prepared_by_id[did]
        kind = p["kind"]
        choice = d.get("choice")
        valid = VALID_CHOICE_FOR_KIND.get(kind)
        if valid is None:
            # Dynamic options — check against prepared options
            valid = set(p.get("options", []))
        if choice not in valid:
            errors.append(f"decision {did!r}: choice {choice!r} not valid for kind {kind!r}; valid: {sorted(valid)}")
        # Round-trip check: decision must include original kind (and variants for
        # conflicting_new_entity_proposals)
        if d.get("kind") != kind:
            errors.append(f"decision {did!r}: kind mismatch ({d.get('kind')!r} vs {kind!r})")
        if kind == "conflicting_new_entity_proposals":
            if "variants" not in d:
                errors.append(
                    f"decision {did!r}: conflicting_new_entity_proposals requires 'variants' array in decision payload"
                )
    return errors


def apply_merge(temp_dir: Path, payload: dict):
    """Apply auto_apply + decisions transactionally.

    On any validation error: exit non-zero, NO mutation, NO hash recording.
    """
    glossary = load_glossary(temp_dir)

    # Re-run prepare-merge to validate that the decisions match the current state
    prepared = prepare_merge(temp_dir)
    decisions = payload.get("decisions", [])
    auto_apply = payload.get("auto_apply", prepared.get("auto_apply", []))
    consumed_chunk_ids = payload.get("consumed_chunk_ids", prepared.get("consumed_chunk_ids", []))

    # Validate decisions
    errors = validate_decisions(decisions, prepared)
    if errors:
        print("APPLY-MERGE ABORTED: validation errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    # ── Phase 1: apply auto_apply items ─────────────────────────────
    for item in auto_apply:
        action = item.get("action")
        if action == "add_entity":
            # Check surface-form uniqueness (no two terms with same source/alias)
            new_src = item["source"]
            new_tgt = item["target"]
            new_id = new_src  # use source as id (matches glossary.py convention)
            if find_existing_term(glossary, new_src):
                # Already exists — skip (race condition between prepare and apply)
                continue
            # Check new_src isn't already an alias of another term
            for t in glossary["terms"]:
                if new_src.lower() in [a.lower() for a in t.get("aliases", [])]:
                    # Promote: remove from aliases, add as standalone
                    t["aliases"] = [a for a in t.get("aliases", []) if a.lower() != new_src.lower()]
                    break
            glossary["terms"].append(
                {
                    "id": new_id,
                    "source": new_src,
                    "target": new_tgt,
                    "aliases": [],
                    "category": item.get("category", "other"),
                    "gender": "unknown",
                    "confidence": "low",  # auto-applied -> low until human confirms
                    "frequency": 0,
                    "evidence_refs": item.get("evidence_refs", []),
                    "notes": "auto-applied from sub-agent meta",
                }
            )
        elif action == "add_alias":
            variant = item["variant"]
            to_src = item["to_source"]
            term = find_existing_term(glossary, to_src)
            if not term:
                continue  # entity gone — skip
            if variant.lower() in [a.lower() for a in term.get("aliases", [])]:
                continue
            # Surface-form uniqueness: variant must not be a source of another term
            if find_existing_term(glossary, variant):
                # Conflict — skip auto-apply; should have been a decision
                continue
            term.setdefault("aliases", []).append(variant)
        elif action == "set_attribute":
            term = find_existing_term(glossary, item["entity_source"])
            if not term:
                continue
            term[item["attribute"]] = item["value"]
        else:
            print(f"WARN: unknown auto_apply action {action!r}", file=sys.stderr)

    # ── Phase 2: apply decisions ────────────────────────────────────
    prepared_by_id = {d["id"]: d for d in prepared.get("decisions_needed", [])}
    for d in decisions:
        did = d.get("id")
        p = prepared_by_id.get(did)
        if not p:
            continue  # already validated as error above; shouldn't reach
        kind = p["kind"]
        choice = d.get("choice")

        if kind == "alias":
            if choice == "yes_alias":
                variant = p["variant"]
                candidate = p["candidate_source"]
                term = find_existing_term(glossary, candidate)
                if term and not find_existing_term(glossary, variant):
                    if variant.lower() not in [a.lower() for a in term.get("aliases", [])]:
                        term.setdefault("aliases", []).append(variant)
            # no_separate_entity / skip -> nothing to do

        elif kind == "conflict":
            entity = p["entity_source"]
            field = p["field"]
            term = find_existing_term(glossary, entity)
            if not term:
                continue
            if choice == "accept_proposed":
                old = term.get(field)
                term[field] = p["proposed"]
                term.setdefault("notes", "")
                if term["notes"]:
                    term["notes"] += " | "
                term["notes"] += f"was {field}={old!r}, changed via conflict decision"
            elif choice == "record_in_notes":
                term.setdefault("notes", "")
                if term["notes"]:
                    term["notes"] += " | "
                term["notes"] += f"conflict on {field}: kept {p['current']!r}, proposed {p['proposed']!r}"
            # keep_current -> nothing

        elif kind == "existing_entity_conflict":
            entity = p["entity_source"]
            term = find_existing_term(glossary, entity)
            if not term:
                continue
            if choice == "keep_current":
                pass
            elif choice == "record_in_notes":
                term.setdefault("notes", "")
                if term["notes"]:
                    term["notes"] += " | "
                term["notes"] += (
                    f"conflict on target: kept {p['current_target']!r}, "
                    f"proposals: {[v['target_proposal'] for v in p['proposed_variants']]}"
                )
            elif choice.startswith("use_variant_"):
                idx = int(choice.split("_")[-1])
                variant = p["proposed_variants"][idx]
                old_target = term.get("target")
                old_category = term.get("category")
                term["target"] = variant["target_proposal"]
                term["category"] = variant["category"]
                term.setdefault("notes", "")
                if term["notes"]:
                    term["notes"] += " | "
                term["notes"] += (
                    f"target was {old_target!r} ({old_category!r}), "
                    f"changed to {variant['target_proposal']!r} ({variant['category']!r})"
                )

        elif kind == "conflicting_new_entity_proposals":
            src = p["source"]
            if choice == "skip":
                continue
            if not find_existing_term(glossary, src):
                if choice.startswith("use_variant_"):
                    idx = int(choice.split("_")[-1])
                    variant = p["variants"][idx]
                    glossary["terms"].append(
                        {
                            "id": src,
                            "source": src,
                            "target": variant["target_proposal"],
                            "aliases": [],
                            "category": variant["category"],
                            "gender": "unknown",
                            "confidence": "low",
                            "frequency": 0,
                            "evidence_refs": variant.get("evidence_chunks", []),
                            "notes": "added via conflicting_new_entity_proposals decision",
                        }
                    )

        elif kind == "attribute_low_confidence":
            if choice == "accept":
                term = find_existing_term(glossary, p["entity_source"])
                if term:
                    term[p["attribute"]] = p["proposed_value"]

        elif kind == "attribute_conflict":
            term = find_existing_term(glossary, p["entity_source"])
            if not term:
                continue
            if choice == "accept_proposed":
                term[p["attribute"]] = p["proposed_value"]
            elif choice == "record_in_notes":
                term.setdefault("notes", "")
                if term["notes"]:
                    term["notes"] += " | "
                term["notes"] += (
                    f"attr conflict {p['attribute']}: kept {p['current_value']!r}, proposed {p['proposed_value']!r}"
                )

    # ── Phase 3: record consumed meta hashes ────────────────────────
    # CRITICAL: even if auto_apply and decisions are empty, we MUST record
    # the hashes for consumed_chunk_ids — otherwise no-op metas will be
    # re-scanned forever.
    from config import read_text_safe

    for cid in consumed_chunk_ids:
        mp = process_dir(temp_dir) / f"output_{cid}.meta.json"
        if mp.exists():
            meta_hash = stable_hash(read_text_safe(mp))
            glossary["applied_meta_hashes"][cid] = meta_hash

    # ── Atomic write ────────────────────────────────────────────────
    save_glossary_atomic(temp_dir, glossary)

    print(
        json.dumps(
            {
                "auto_applied": len(auto_apply),
                "decisions_resolved": len(decisions),
                "consumed_chunks": len(consumed_chunk_ids),
                "errors": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


# ─────────────────────────────────────────────────────────────────────
# status
# ─────────────────────────────────────────────────────────────────────


def status(temp_dir: Path):
    glossary = load_glossary(temp_dir)
    applied_hashes = glossary.get("applied_meta_hashes", {})

    meta_files = sorted(process_dir(temp_dir).glob("output_chunk*.meta.json"))
    output_files = sorted(process_dir(temp_dir).glob("output_chunk*.md"))

    consumed = 0
    unmerged = 0
    malformed = 0
    missing_meta = 0

    from config import read_text_safe

    for mp in meta_files:
        cid = mp.name.replace("output_", "").replace(".meta.json", "")
        try:
            meta = json.loads(read_text_safe(mp))
            _, errors = validate_meta(meta)
            if errors:
                malformed += 1
                continue
        except json.JSONDecodeError:
            malformed += 1
            continue
        meta_hash = stable_hash(read_text_safe(mp))
        if applied_hashes.get(cid) == meta_hash:
            consumed += 1
        else:
            unmerged += 1

    # Outputs without corresponding meta file
    output_cids = {f.name.replace("output_", "").replace(".md", "") for f in output_files}
    meta_cids = {f.name.replace("output_", "").replace(".meta.json", "") for f in meta_files}
    missing_meta = len(output_cids - meta_cids)

    print(f"Meta merge status: {temp_dir}")
    print(f"  Output chunks:           {len(output_files)}")
    print(f"  Meta files found:        {len(meta_files)}")
    print(f"  Consumed (in glossary):  {consumed}")
    print(f"  Unmerged (pending):      {unmerged}")
    print(f"  Malformed:               {malformed}")
    print(f"  Outputs missing meta:    {missing_meta}")
    print()
    if unmerged > 0:
        print("Unmerged chunks need merge_meta.py prepare-merge + apply-merge.")
    if malformed > 0:
        print("Malformed meta files are quarantined — fix or delete them.")
    if missing_meta > 0:
        print("Some chunks have no meta file — sub-agent compliance issue.")
    severity_issues = []
    if unmerged > 0:
        severity_issues.append("unmerged_meta_files > 0 — bug if Step 6 ran")
    if malformed > 0:
        severity_issues.append("malformed_meta_files > 0 — fix by hand")
    if missing_meta > 0:
        severity_issues.append("meta_files_found < translated_chunks")
    if severity_issues:
        print()
        print("Severity flags:")
        for s in severity_issues:
            print(f"  - {s}")


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "prepare-merge":
        if len(sys.argv) < 3:
            print("Usage: merge_meta.py prepare-merge <temp_dir>")
            sys.exit(1)
        result = prepare_merge(Path(sys.argv[2]))
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "apply-merge":
        if len(sys.argv) < 3:
            print("Usage: echo '<json>' | merge_meta.py apply-merge <temp_dir>")
            sys.exit(1)
        temp_dir = Path(sys.argv[2])
        payload_text = sys.stdin.read()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as e:
            print(f"ERROR: invalid JSON on stdin: {e}", file=sys.stderr)
            sys.exit(1)
        apply_merge(temp_dir, payload)

    elif cmd == "status":
        if len(sys.argv) < 3:
            print("Usage: merge_meta.py status <temp_dir>")
            sys.exit(1)
        status(Path(sys.argv[2]))

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
