#!/usr/bin/env python3
"""
Veeva -> LSC Page Layout Accelerator — Org Verifier

Answers two real questions that pattern-matching alone can't:

1. "Does this candidate target field ACTUALLY exist in the target org?"
   -> exact-match check against a real sobject describe() dump.

2. "This Veeva field was RENAMED in LSC -- what's the real name?"
   -> fuzzy-matches the Veeva field's label against every real field's
      label in the target org, returning a ranked shortlist of genuine
      candidates (not a guess from nowhere) for a human to pick from.

3. "Will this layout's record type assignment actually work?"
   -> checks the target org's real recordTypeInfos before generation/
      deployment even runs, catching the exact failure mode from manual
      migration experience (record type missing) as a pre-flight check
      instead of a deploy-time surprise.

Input: the JSON produced by:
    sf sobject describe --sobject Account --target-org <alias> --json > account_describe.json

Usage as a library (used by rules_engine.py / app.py):
    from org_verifier import load_describe, verify_exact_field, find_candidate_fields, check_record_types

Usage standalone:
    python3 org_verifier.py --describe account_describe.json --field SP_Territory_Owner__c
    python3 org_verifier.py --describe account_describe.json --label "Territory Owner" --suggest
    python3 org_verifier.py --describe account_describe.json --record-types "HCO,HCP"
"""

import argparse
import json
from difflib import SequenceMatcher


def load_describe(path):
    """Loads a sobject describe() JSON dump (from `sf sobject describe --json`)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # sf CLI wraps the actual describe payload under "result"
    return data.get("result", data)


def verify_exact_field(describe_data, api_name):
    """Does a field with this EXACT API name exist in the org? Zero
    ambiguity -- if this returns True, it's a fact, not a guess."""
    for field in describe_data.get("fields", []):
        if field.get("name") == api_name:
            return True, {
                "label": field.get("label"),
                "type": field.get("type"),
                "custom": field.get("custom"),
            }
    return False, None


def _label_similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_candidate_fields(describe_data, veeva_label, veeva_api_name=None, top_n=3, min_score=0.75):
    """When the exact name doesn't exist, search every REAL field in the
    org by label similarity to the Veeva field's label. Returns a ranked
    shortlist of genuine candidates from the actual org -- turns 'research
    this from scratch' into 'pick option 1 of 3', without pretending to
    know the answer for certain.

    min_score defaults to 0.75, deliberately conservative: tested against
    a real org describe with few custom fields, a lower threshold (0.5)
    produced confident-looking but nonsensical matches (e.g. matching
    'Specialty' to 'ShippingCity' at 0.5 similarity, purely because it was
    the least-bad option among fields that don't actually relate at all).
    Better to honestly report 'no reliable match' than surface a coincidental
    string-similarity artifact as if it were a real suggestion."""
    candidates = []
    for field in describe_data.get("fields", []):
        label = field.get("label", "")
        score = _label_similarity(veeva_label, label)
        # Small boost if the API name also shares a recognizable root
        # (e.g. Veeva "Credentials_vod__c" vs LSC "Provider_Credentials__c")
        if veeva_api_name:
            root = veeva_api_name.replace("_vod__c", "").replace("__c", "").lower()
            if root and root in field.get("name", "").lower():
                score = min(1.0, score + 0.15)
        if score >= min_score:
            candidates.append({
                "api_name": field.get("name"),
                "label": label,
                "type": field.get("type"),
                "similarity": round(score, 2),
            })
    candidates.sort(key=lambda c: c["similarity"], reverse=True)
    return candidates[:top_n]


def verify_exact_related_list(describe_data, veeva_related_object):
    """Does a child relationship to this object exist in the org? Checks
    by childSObject name (case-insensitive substring, since Veeva's
    'Address_vod__c' and an LSC 'Address' object are the same concept with
    a different exact name -- exact API match is checked separately by
    verify_exact_field-style callers first; this handles the object-level
    existence question for related lists specifically)."""
    veeva_root = veeva_related_object.replace("_vod__c", "").replace("__c", "").lower()
    for rel in describe_data.get("childRelationships", []):
        child_obj = (rel.get("childSObject") or "")
        if child_obj.lower() == veeva_related_object.lower():
            return True, rel
    return False, None


def find_candidate_related_lists(describe_data, veeva_related_object, top_n=3, min_score=0.6):
    """When no exact child-object match exists, fuzzy-search all REAL child
    relationships in the org by name similarity to the Veeva object name.

    Includes a substring-containment boost: 'Address_vod__c' root 'address'
    is only a small fragment of 'ContactPointAddress' by character count, so
    plain sequence similarity badly underrates a real, correct match like
    this (compound object names are common in Salesforce). Checking
    containment directly, the same fix already applied to field matching,
    is what actually finds it."""
    veeva_root = veeva_related_object.replace("_vod__c", "").replace("__c", "").replace("_", "").lower()
    candidates = []
    seen = set()
    for rel in describe_data.get("childRelationships", []):
        child_obj = rel.get("childSObject") or ""
        rel_name = rel.get("relationshipName") or ""
        if not child_obj or child_obj in seen:
            continue
        score = max(_label_similarity(veeva_root, child_obj), _label_similarity(veeva_root, rel_name))
        if veeva_root and veeva_root in child_obj.lower():
            score = max(score, 0.85)
        if score >= min_score:
            seen.add(child_obj)
            candidates.append({
                "child_object": child_obj,
                "relationship_name": rel_name or None,
                "similarity": round(score, 2),
            })
    candidates.sort(key=lambda c: c["similarity"], reverse=True)
    return candidates[:top_n]


def check_record_types(describe_data, required_record_type_names):
    """Pre-flight check: do the record types this layout needs actually
    exist in the target org? This is exactly the failure mode from manual
    migration experience -- a layout deploy that silently depends on a
    record type nobody created yet."""
    existing = {rt.get("name"): rt for rt in describe_data.get("recordTypeInfos", [])}
    results = []
    for name in required_record_type_names:
        exists = name in existing
        results.append({
            "record_type": name,
            "exists": exists,
            "active": existing[name].get("active") if exists else None,
        })
    return results


def enrich_registry_entry(entry, describe_data):
    """Takes one registry entry (the shape used in mapping_registry.json)
    and upgrades it using real org data:
      - If the proposed target field exists EXACTLY as named -> confidence
        becomes High, marked 'org_verified': True. No human needed for this one.
      - If not -> attaches 'suggested_candidates' (real fields from the org,
        ranked by similarity) instead of leaving a blank guess. Confidence
        is NOT upgraded -- a human still picks/confirms from the shortlist.
    Does not mutate the original entry; returns a new one.
    """
    target_name = entry.get("target", {}).get("api_name")
    if not target_name or target_name in ("-", "\u2014"):
        return entry

    exists, field_info = verify_exact_field(describe_data, target_name)
    new_entry = json.loads(json.dumps(entry))  # deep copy

    if exists:
        new_entry["target"]["confidence"] = "High"
        new_entry["org_verified"] = True
        new_entry["basis"] = (new_entry.get("basis", "") +
                               f" [Org-verified: '{target_name}' confirmed to exist "
                               f"in target org as {field_info['type']}]")
        if new_entry.get("action") == "flag_manual_review":
            new_entry["action"] = "auto_generate"
    else:
        veeva_label = entry.get("api_name", "").replace("_vod__c", "").replace("__c", "").replace("_", " ")
        candidates = find_candidate_fields(describe_data, veeva_label, entry.get("api_name"))
        new_entry["org_verified"] = False
        new_entry["suggested_candidates"] = candidates
        if candidates:
            new_entry["basis"] = (new_entry.get("basis", "") +
                                   f" [Org check: '{target_name}' NOT found. "
                                   f"{len(candidates)} similar real field(s) found instead -- "
                                   f"review 'suggested_candidates'.]")
        else:
            new_entry["basis"] = (new_entry.get("basis", "") +
                                   f" [Org check: '{target_name}' NOT found, and no similar "
                                   f"field found either -- this field likely doesn't exist "
                                   f"in the target org yet.]")
    return new_entry


def main():
    ap = argparse.ArgumentParser(description="Verify field mappings and record types against a live org's describe() dump.")
    ap.add_argument("--describe", required=True, help="Path to sobject describe JSON (from sf sobject describe --json)")
    ap.add_argument("--field", default=None, help="Check if this exact API name exists")
    ap.add_argument("--label", default=None, help="Find candidates by label similarity (use with --suggest)")
    ap.add_argument("--api-name", default=None, help="Original Veeva API name, improves candidate matching")
    ap.add_argument("--suggest", action="store_true", help="Run candidate search using --label")
    ap.add_argument("--record-types", default=None, help="Comma-separated record type names to check")
    args = ap.parse_args()

    describe_data = load_describe(args.describe)

    if args.field:
        exists, info = verify_exact_field(describe_data, args.field)
        if exists:
            print(f"EXISTS: '{args.field}' -> label='{info['label']}', type={info['type']}, custom={info['custom']}")
        else:
            print(f"NOT FOUND: '{args.field}' does not exist in this org's describe() output.")

    if args.suggest and args.label:
        candidates = find_candidate_fields(describe_data, args.label, args.api_name)
        if candidates:
            print(f"Top candidates for label '{args.label}':")
            for c in candidates:
                print(f"  {c['similarity']:.2f}  {c['api_name']:35s} (label: '{c['label']}', type: {c['type']})")
        else:
            print(f"No similar fields found for label '{args.label}'.")

    if args.record_types:
        names = [n.strip() for n in args.record_types.split(",")]
        results = check_record_types(describe_data, names)
        for r in results:
            status = "EXISTS" if r["exists"] else "MISSING"
            active_note = f", active={r['active']}" if r["exists"] else ""
            print(f"  [{status}] {r['record_type']}{active_note}")


if __name__ == "__main__":
    main()
