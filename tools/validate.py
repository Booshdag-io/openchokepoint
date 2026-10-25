#!/usr/bin/env python3
# Handling: Unclassified — public
# SPDX-License-Identifier: Apache-2.0
"""Validate instance files against the OpenChokepoint schema.

Usage:
    python3 tools/validate.py [--tier public|private] [instance.yaml ...]

--tier public (default) is for this public repository and its CI. --tier private is for private clones that hold full data.

Defaults to the synthetic examples. Requires PyYAML. Exits non-zero on any error.
Records outside data/ must carry `synthetic: true`. Real records are accepted only under data/ with a provenance.yaml (see DATA-CONTRIBUTIONS.md).

Schema v0.2 adds digital-thread rules. Each error starts with a stable rule ID so reviewers can cite it:
  EV-001  EvidenceRecord must carry a content hash or a public citation
  EV-002  EvidenceRecord must not carry inline content; content_hash must be sha256:<64 hex>; short strings only
  EV-003  EvidenceRecord citation must be a public https URL
  EV-004  A real EvidenceRecord must use a public citation (public reports only)
  CI-001  CriticalItem must be synthetic: true
  CI-002  CriticalItem must not flag, or be offered an alternate by, a real record
  CI-003  CriticalItem must not reach a real record through its target (mixed graphs)
  CI-004  CriticalItem flags must be consistent with the graph (alternate linked; missing evidence really missing)
  DT-001  underwent.sequence must be unique per SerialItem
  DT-002  Real digital-thread records must be graded confirmed or reported (gaps are shown, never inferred)
  DT-003  Public tier: a real underwent link must not carry a sequence (steps that happened, not their order)
  DT-004  An underwent link needs a sequence, except a real link in the public tier
"""
import datetime
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ISO3166 = re.compile(r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")
URL = re.compile(r"^https?://\S+$")


def load(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def check_value(spec, value, label, errors, known_ids):
    vtype = spec["type"]
    values = value if spec.get("many") else [value]
    if spec.get("many") and (not isinstance(value, list) or not value):
        errors.append(f"{label}: expected a non-empty list")
        return
    for v in values:
        ok = {
            "string": lambda x: isinstance(x, str) and x.strip() != "",
            "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
            "decimal": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
            "boolean": lambda x: isinstance(x, bool),
            "date": lambda x: isinstance(x, datetime.date),
            "iso3166": lambda x: isinstance(x, str) and bool(ISO3166.match(x)),
            "url": lambda x: isinstance(x, str) and bool(URL.match(x)),
            "enum": lambda x: x in spec.get("values", []),
            "ref": lambda x: x in known_ids,
        }[vtype](v)
        if not ok:
            errors.append(f"{label}: value {v!r} is not a valid {vtype}")


def check_props(record, props, label, errors, known_ids, reserved):
    for name, spec in props.items():
        if name in record:
            check_value(spec, record[name], f"{label}.{name}", errors, known_ids)
        elif spec.get("required"):
            errors.append(f"{label}: missing required property '{name}'")
    for name in record:
        if name not in props and name not in reserved:
            errors.append(f"{label}: unknown property '{name}'")


PERSONAL = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}|\(?\b\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")


def check_public_data(path, records):
    """Real (non-synthetic) records are accepted only under data/, with a complete provenance.yaml,
    public http(s) sources on every record, and no personal contact details (DATA-CONTRIBUTIONS.md)."""
    errs = []
    rel = path.resolve().relative_to(ROOT) if path.resolve().is_relative_to(ROOT) else path
    if not str(rel).startswith("data/"):
        return [f"{path}: real records are accepted only under data/ — everything else must be synthetic"]
    prov_path = path.parent / "provenance.yaml"
    if not prov_path.exists():
        return [f"{path}: real records need a provenance.yaml in the same folder"]
    prov = load(prov_path) or {}
    if prov.get("personal_information") != "none":
        errs.append(f"{prov_path}: personal_information must be 'none'")
    if prov.get("export_controlled") is not False:
        errs.append(f"{prov_path}: export_controlled must be false")
    if not prov.get("sources"):
        errs.append(f"{prov_path}: sources missing")
    for rec in records:
        if rec.get("synthetic") is not False:
            errs.append(f"{rec.get('id')}: set synthetic: false explicitly on real records")
        if rec.get("evidence_grade") in ("speculative", "no_evidence"):
            errs.append(f"{rec.get('id')}: real records need evidence grade confirmed, reported or inferred")
        for s in rec.get("sources") or []:
            if not str(s).startswith("https://"):
                errs.append(f"{rec.get('id')}: sources must be public https URLs")
        if PERSONAL.search(" ".join(str(v) for v in rec.values() if isinstance(v, str))):
            errs.append(f"{rec.get('id')}: looks like an email address or phone number — no personal contact details")
    return errs


THREAD_TYPES = {"Component", "SerialItem", "MaterialLot", "ProcessStep", "EvidenceRecord"}
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")

# Red-team rules (Slice C - RT series): prevent smuggling of controlled technical data
# (temperatures, pressures, tolerances, dimensions, engineering units)
TECHNICAL_DATA_RE = re.compile(
    r"(?i)\d+(?:\.\d+)?\s*(?:deg\s*[cf]|kpa|mpa|psi|bar|rpm|hrc|m/s|mm|cm|microns?|in(?:ches)?|ft|lbs?|kg|hz|khz|ghz|v|mv|kv|ma|kw|mw)|"
    r"(?i)(?:tolerance|clearance)\s*[:=]?\s*[+-]?\d+(?:\.\d+)?|"
    r"(?i)\d+(?:\.\d+)?\s*[±\+/-]\s*\d+(?:\.\d+)?"
)

INLINE_CONTENT = {"content", "body", "text", "data", "attachment", "payload", "excerpt", "value", "values",
                  "result", "results", "measurements", "parameters", "notes"}
EVIDENCE_STRING_MAX = 120   # pointers are short; anything longer looks like content (EV-002)


def is_real(rec):
    return rec.get("synthetic") is not True


def check_thread(objects, links, tier="public"):
    """Digital-thread and critical-item rules (schema v0.2). Fail closed; every error names the rule and the record."""
    errs = []
    by_id = {r.get("id"): r for r in objects + links}


    # RT-001: Technical data and engineering measurement smuggling check across all fields
    for o in objects + links:
        rid = o.get("id", "unknown")
        for k, v in o.items():
            if isinstance(v, str):
                m = TECHNICAL_DATA_RE.search(v)
                if m:
                    errs.append(f"RT-001 {rid}: property '{k}' contains prohibited technical/engineering data ('{m.group(0)}')")
            elif isinstance(v, list):
                for idx, item in enumerate(v):
                    if isinstance(item, str):
                        m = TECHNICAL_DATA_RE.search(item)
                        if m:
                            errs.append(f"RT-001 {rid}: property '{k}[{idx}]' contains prohibited technical/engineering data ('{m.group(0)}')")

    # EvidenceRecord: pointer only.
    for o in objects:
        if o.get("type") != "EvidenceRecord":
            continue
        rid = o.get("id")
        has_hash, has_cite = "content_hash" in o, "citation" in o
        if not (has_hash or has_cite):
            errs.append(f"EV-001 {rid}: EvidenceRecord needs content_hash or citation (a pointer to the record, never the record)")
        bad = sorted(INLINE_CONTENT & set(o))
        if bad:
            errs.append(f"EV-002 {rid}: inline content field(s) {bad} not allowed; point to the record with content_hash or citation")
        if has_hash and not SHA256.match(str(o["content_hash"])):
            errs.append(f"EV-002 {rid}: content_hash must be sha256:<64 lowercase hex>")
        for k in ("issuer", "citation_section", "name"):
            if isinstance(o.get(k), str) and len(o[k]) > EVIDENCE_STRING_MAX:
                errs.append(f"EV-002 {rid}: '{k}' longer than {EVIDENCE_STRING_MAX} characters looks like content, not a pointer")
        if has_cite and not str(o["citation"]).startswith("https://"):
            errs.append(f"EV-003 {rid}: citation must be a public https URL")
        if is_real(o) and not has_cite:
            errs.append(f"EV-004 {rid}: a real EvidenceRecord must cite a publicly released report (citation), not only a hash")

    # Real digital-thread records: no inference.
    for o in objects:
        if o.get("type") in THREAD_TYPES and is_real(o) and o.get("evidence_grade") not in ("confirmed", "reported"):
            errs.append(f"DT-002 {o.get('id')}: real digital-thread records must be graded confirmed or reported; show gaps, do not infer")

    # underwent.sequence: unique per SerialItem; real links carry none in the public tier.
    seen = {}
    for l in links:
        if l.get("type") == "underwent":
            if tier == "public" and is_real(l) and "sequence" in l:
                errs.append(f"DT-003 {l.get('id')}: a real step link must not carry a sequence in the public tier; record that the step happened, not its order")
            elif "sequence" not in l and not (tier == "public" and is_real(l)):
                errs.append(f"DT-004 {l.get('id')}: underwent link needs a sequence")
            if "sequence" not in l:
                continue
            key = (l.get("from"), l.get("sequence"))
            if key in seen:
                errs.append(f"DT-001 {l.get('id')}: sequence {l.get('sequence')} already used for {l.get('from')} by {seen[key]}")
            seen[key] = l.get("id")

    # CriticalItem: synthetic only, never touching real records.
    neighbours = {}
    for l in links:
        a, b = l.get("from"), l.get("to")
        neighbours.setdefault(a, set()).add(b)
        neighbours.setdefault(b, set()).add(a)
    evidenced_steps = {l.get("from") for l in links if l.get("type") == "evidenced_by"}

    for o in objects:
        if o.get("type") != "CriticalItem":
            continue
        cid = o.get("id")
        if is_real(o):
            errs.append(f"CI-001 {cid}: CriticalItem is a judgment and must be synthetic: true")
        flag_links = [l for l in links if l.get("type") == "flags" and l.get("from") == cid]
        alt_links = [l for l in links if l.get("type") == "alternate_for" and l.get("to") == cid]
        targets = [l.get("to") for l in flag_links]
        for l in flag_links + alt_links:
            if is_real(l):
                errs.append(f"CI-002 {l.get('id')}: links to or from a CriticalItem must be synthetic: true")
        for t in targets:
            if t in by_id and is_real(by_id[t]):
                errs.append(f"CI-002 {cid}: flags real record {t}; judgments are never recorded against a real organization or part")
        for l in alt_links:
            src = l.get("from")
            if src in by_id and is_real(by_id[src]):
                errs.append(f"CI-002 {cid}: alternate_for from real record {src}; judgments are never recorded against a real organization")
        for t in targets:
            if t in by_id and is_real(by_id[t]):
                continue
            for n in sorted(neighbours.get(t, set()) - {cid}):
                if n in by_id and is_real(by_id[n]) and by_id[n].get("type") != "CriticalItem":
                    errs.append(f"CI-003 {cid}: target {t} is linked to real record {n}; a synthetic flag cannot reach a real organization or part")
        if not flag_links:
            errs.append(f"CI-004 {cid}: CriticalItem must flag at least one item")
        if o.get("qualified_alternate") is True and not alt_links:
            errs.append(f"CI-004 {cid}: qualified_alternate is true but no alternate_for link names the alternate")
        if o.get("single_source") is True and (alt_links or o.get("qualified_alternate") is True):
            errs.append(f"CI-004 {cid}: single_source contradicts a qualified alternate")
        if o.get("missing_evidence") is True:
            steps = [t for t in targets if by_id.get(t, {}).get("type") == "ProcessStep"]
            if not steps:
                errs.append(f"CI-004 {cid}: missing_evidence is true but no ProcessStep is flagged")
            for t in steps:
                if t in evidenced_steps:
                    errs.append(f"CI-004 {cid}: missing_evidence is true but step {t} has an EvidenceRecord")
    return errs


def validate(instance_path, onto, tier="public"):
    errors = []
    data = load(instance_path)
    shared = onto["shared_properties"]

    objects = data.get("objects", [])
    links = data.get("links", [])
    all_ids = [r.get("id") for r in objects + links]
    dupes = {i for i in all_ids if all_ids.count(i) > 1}
    for d in dupes:
        errors.append(f"duplicate id '{d}'")
    types_by_id = {o.get("id"): o.get("type") for o in objects}

    real = [r for r in objects + links if r.get("synthetic") is not True]
    if real:
        errors.extend(check_public_data(Path(instance_path), real))

    for obj in objects:
        label = f"object {obj.get('id')}"
        otype = onto["object_types"].get(obj.get("type"))
        if not otype:
            errors.append(f"{label}: unknown object type '{obj.get('type')}'")
            continue
        props = {**shared, **otype.get("properties", {})}
        check_props(obj, props, label, errors, types_by_id, {"type"})

    for link in links:
        label = f"link {link.get('id')}"
        ltype = onto["link_types"].get(link.get("type"))
        if not ltype:
            errors.append(f"{label}: unknown link type '{link.get('type')}'")
            continue
        props = {**shared, **ltype.get("properties", {})}
        check_props(link, props, label, errors, types_by_id, {"type", "from", "to"})
        for end in ("from", "to"):
            target = link.get(end)
            if target not in types_by_id:
                errors.append(f"{label}: '{end}' references unknown object '{target}'")
            elif types_by_id[target] not in ltype[end]:
                errors.append(f"{label}: '{end}' type {types_by_id[target]} not allowed (expects {ltype[end]})")

    errors.extend(check_thread(objects, links, tier))
    return errors


def main(argv):
    args = argv[1:]
    tier = "public"
    if "--tier" in args:
        i = args.index("--tier")
        if i + 1 >= len(args) or args[i + 1] not in ("public", "private"):
            print("usage: validate.py [--tier public|private] [instance.yaml ...]")
            return 2
        tier = args[i + 1]
        del args[i:i + 2]
    onto = load(ROOT / "schema" / "openchokepoint.yaml")
    targets = args or sorted((ROOT / "examples").glob("*.yaml"))
    failed = False
    for target in targets:
        errors = validate(target, onto, tier)
        if errors:
            failed = True
            print(f"FAIL {target}")
            for e in errors:
                print(f"  - {e}")
        else:
            print(f"OK   {target}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
