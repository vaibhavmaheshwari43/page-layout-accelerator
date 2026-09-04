#!/usr/bin/env python3
"""
Veeva -> LSC Page Layout Accelerator — Layer 3: Rules Engine

Takes raw elements from Layer 1 (extractor.py) and makes the actual build
decisions: classification (5-way Decision Matrix taxonomy), target, and
action — using the Mapping Registry as the source of truth, and falling
back to explicit structural rules only when the registry has no entry.

This is the ONLY layer where judgment calls live. Layer 1 stays a dumb
parser; Layer 4 (Generation) just reads this layer's "action" field and
builds or flags accordingly — it doesn't re-derive classification itself.

Design principle carried over from the project spec:
  - Registry entries (human-reviewed) always win over structural defaults.
  - Any element the registry doesn't cover is flagged
    ("registry_match": false, action "flag_no_registry_entry") — never
    silently guessed and never crashes the run.
  - Structural rules are object-agnostic (keyed by element_type / section
    label patterns, not by object-specific field names), so this same file
    runs unchanged against HCP or any future layout.

Usage:
    python rules_engine.py --object Account --layout SP_Admin_Layout_HCO \
        --xml Veeva_Source_Account-SP_Admin_Layout_HCO_layout-meta.xml \
        --registry mapping_registry.json \
        --out classified_SP_Admin_Layout_HCO.json
"""

import argparse
import json
from collections import defaultdict

from extractor import extract_all

# ---------------------------------------------------------------------------
# Exception list: standard Salesforce sections that render implicitly even
# when empty in the Veeva source metadata. This is a maintained list, not a
# structural inference — new exceptions get added here as new layouts
# surface them, never inferred silently.
#
# Each entry names the actual field that populates the section, verified
# against the real built HCO reference file — NOT guessed. Generation needs
# this to actually build the section; classifying the shell "Direct" alone
# isn't enough content to generate from (caught during validation: the first
# generator run produced 0 fields for this section because nothing tells it
# WHAT to place there).
# ---------------------------------------------------------------------------
EMPTY_SECTION_RENDER_EXCEPTIONS = {
    "Description Information": {
        "field": "Description",
        "behavior": "Edit",
        "basis": "Confirmed in actual LSC target Page Layout file — Description "
                 "Information section is empty in Veeva source metadata but "
                 "Salesforce renders the standard Description field there.",
    },
}


def classify_empty_section(label):
    if label in EMPTY_SECTION_RENDER_EXCEPTIONS:
        return "Direct", "Standard section renders implicitly despite empty metadata (known exception)."
    return "Decision Needed", (
        "Empty in metadata — Veeva may inject content at runtime (VMocs/dynamic). "
        "Confirm what actually renders here before assuming coverage elsewhere or dropping."
    )


def structural_default_classification(el):
    """Applied ONLY when the registry has no entry for this element."""
    et = el["element_type"]

    if et == "Field":
        return "Decision Needed", "Field not yet in Mapping Registry — no known target. Needs mapping before build."
    if et == "Custom Link":
        return "Rebuild", "Veeva JS custom link — will not function in Lightning as-is. Needs native equivalent or rebuild."
    if et == "Embedded Lightning Page":
        return "Rebuild", "Veeva-managed embedded component — no native LSC equivalent. Needs custom LWC."
    if et == "Section (empty in metadata)":
        return classify_empty_section(el["layout_section"])
    if et == "Related List":
        return "Decision Needed", "Related list not yet in Mapping Registry — needs explicit target or scope decision."
    if et == "Custom Button":
        return "Decision Needed", "Custom button not yet classified — needs scope decision (in/out of POC) before build."
    if et == "Quick Action":
        return "Rebuild", "Veeva Lightning quick action — check for native LSC equivalent before building custom."
    if et == "Setting":
        return "Decision Needed", "Layout setting needs explicit review — org-specific values do not carry over automatically."
    if et in ("_extraction_gap_note", "Unknown layoutItem (needs manual inspection)"):
        return "Decision Needed", "Extraction gap — this row is a flag for a human, not a real layout element."
    return "Decision Needed", "Unrecognized element type — needs manual classification."


ACTION_MAP = {
    "Direct": "auto_generate",
    "Map": "auto_generate",
    "Rebuild": "flag_rebuild",
    "Retire": "flag_retire",
    "Decision Needed": "flag_decision_needed",
}


# Element types whose identity genuinely depends on WHICH section they're in
# (an empty placeholder section has no real name of its own — the section
# label IS its identity), vs. types whose identity is the element itself
# regardless of section placement (a field/button/related list means the
# same thing no matter which section happens to hold it on a given layout).
SECTION_SCOPED_TYPES = {"Section (empty in metadata)"}


def registry_key(element_type, api_name, layout_section):
    if element_type in SECTION_SCOPED_TYPES:
        return (element_type, api_name, layout_section)
    return (element_type, api_name)


def load_registry(path):
    """Builds TWO indices, not one:
    - A GENERIC index: (element_type, api_name) -> entry, same as before.
      Correct fallback for genuinely universal fields (CreatedById,
      LastModifiedById -- same meaning on every object).
    - A SPECIFIC index: (element_type, api_name, veeva_object) -> entry.
      Needed because some fields are legitimately repurposed with a
      DIFFERENT meaning per object -- confirmed real example: 'Name' means
      'Account Name' on Account, but 'Address line 1' on Address_vod__c.
      Grouping these together (the old behavior) silently lost the
      object-specific entry during registry merges.

    classify_elements tries the specific index first (when the current
    layout's source object is known and the entry has that data), and
    only falls back to the generic index otherwise -- so fields without
    tracked object data (like the original hand-built HCO entries) keep
    working exactly as before.

    If multiple rows share the GENERIC key but disagree on
    classification/target, that's flagged via registry_conflicts. Rows
    that differ only because they're genuinely different per-object
    entries (now living in the specific index) are correctly NOT treated
    as conflicts.
    """
    if not path:
        return {}, [], {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    grouped = defaultdict(list)
    specific_index = {}
    for e in data.get("entries", []):
        key = registry_key(e["element_type"], e["api_name"], e["layout_section"])
        grouped[key].append(e)

        veeva_object = e.get("veeva_object")
        if veeva_object and e["element_type"] not in SECTION_SCOPED_TYPES:
            specific_key = (e["element_type"], e["api_name"], veeva_object)
            # If the same (field, object) appears more than once (e.g. same
            # field on multiple layouts under the same object), first one
            # wins -- consistent with the generic index's existing behavior.
            if specific_key not in specific_index:
                specific_index[specific_key] = e

    index = {}
    conflicts = []
    for key, group in grouped.items():
        # A disagreement is only a REAL conflict if it involves two entries
        # that could actually compete for the same lookup at runtime:
        #   - two entries that BOTH lack a veeva_object (both would only
        #     ever serve as the generic fallback -- if they disagree, we
        #     genuinely can't tell which one wins), or
        #   - two entries that share the SAME veeva_object (a genuine
        #     specific-index collision).
        # A generic entry (no object) vs. any object-specific entry is
        # NEVER a real conflict -- classify_elements always tries the
        # specific index first when the object is known, so the generic
        # entry only kicks in as a fallback for objects with no specific
        # entry at all. They don't compete in practice.
        generic_entries = [g for g in group if not g.get("veeva_object")]
        by_object = defaultdict(list)
        for g in group:
            if g.get("veeva_object"):
                by_object[g["veeva_object"]].append(g)

        real_conflict = False
        for entries in [generic_entries] + list(by_object.values()):
            if len(entries) < 2:
                continue
            base = entries[0]
            for other in entries[1:]:
                if (other["classification"]["canonical"] != base["classification"]["canonical"]
                        or other["action"] != base["action"]):
                    real_conflict = True
                    break
            if real_conflict:
                break

        if real_conflict:
            conflicts.append({
                "key": key,
                "sections_involved": [g["layout_section"] for g in group],
                "conflicting_classifications": [g["classification"]["canonical"] for g in group],
                "conflicting_actions": [g["action"] for g in group],
            })
        index[key] = group[0]

    return index, conflicts, specific_index


def determine_plausible_target_objects(raw_elements, registry_index, specific_index, source_object, fallback_object):
    """Figures out which LSC object(s) this layout's fields legitimately
    belong to -- can be MORE THAN ONE, not just a single 'winner'.

    Real, confirmed problem with the single-majority version below: an
    Address_vod__c layout's fields split genuinely and evenly across
    Address (4 fields) and ContactPointPhone (4 fields) -- neither reaches
    a >50% majority, so that logic falls back to comparing against the
    literal Veeva object name 'Address_vod__c', which no real field will
    ever match (that object doesn't exist in LSC at all) -- blocking
    EVERY field on the layout, including ones that were confidently,
    correctly mapped (e.g. City -> Address.City, High confidence).

    Better model: any object that a MEANINGFUL cluster of fields agree on
    (not just a singleton) is treated as legitimate for this layout. Only
    a genuine rare outlier -- a target object basically no other field
    agrees with -- gets flagged.

    Returns a set of object names considered legitimate for this layout.
    """
    from collections import Counter
    target_counts = Counter()
    for el in raw_elements:
        if el["element_type"] != "Field":
            continue
        reg_entry = None
        if source_object and specific_index:
            reg_entry = specific_index.get((el["element_type"], el["api_name"], source_object))
        if reg_entry is None:
            key = registry_key(el["element_type"], el["api_name"], el["layout_section"])
            reg_entry = registry_index.get(key)
        if reg_entry:
            obj = reg_entry["target"].get("object")
            if obj:
                target_counts[obj] += 1

    if not target_counts:
        return {fallback_object} if fallback_object else set()

    total = sum(target_counts.values())
    legitimate = set()
    top_count = target_counts.most_common(1)[0][1]
    for obj, count in target_counts.items():
        if count == top_count or (count >= 2 and count / total >= 0.15):
            legitimate.add(obj)

    return legitimate


def determine_effective_target_object(raw_elements, registry_index, fallback_object):
    """Figures out what most Fields on this layout actually agree the real
    LSC object is -- NOT the raw Veeva object name (which often doesn't
    exist in LSC at all, e.g. 'Call2_vod__c'). Confirmed against real data:
    a Call2_vod__c layout has 362 fields correctly targeting Task, and only
    a genuine minority (38 Account, 23 EventRelation, etc.) targeting
    something else. Comparing against the raw Veeva name would incorrectly
    flag all 464; comparing against the majority (Task) correctly leaves
    only the real ~62 outliers flagged.

    NOTE: kept for backward compatibility; classify_elements itself now
    uses determine_plausible_target_objects (plural) instead, since a
    single 'winner' breaks layouts that legitimately span multiple real
    objects (see that function's docstring).

    Falls back to fallback_object (the object name the tool was actually
    run with) when there's no clear registry data to determine a majority
    from -- e.g. for Account, target.object IS 'Account' for the vast
    majority anyway, so this changes nothing there."""
    from collections import Counter
    target_counts = Counter()
    for el in raw_elements:
        if el["element_type"] != "Field":
            continue
        key = registry_key(el["element_type"], el["api_name"], el["layout_section"])
        reg_entry = registry_index.get(key)
        if reg_entry:
            obj = reg_entry["target"].get("object")
            if obj:
                target_counts[obj] += 1

    if not target_counts:
        return fallback_object

    dominant_object, dominant_count = target_counts.most_common(1)[0]
    total = sum(target_counts.values())
    # Only trust the majority if it's genuinely a majority (>50%), not just
    # the most common of many roughly-equal options.
    if dominant_count / total > 0.5:
        return dominant_object
    return fallback_object


def classify_elements(raw_elements, registry_index, source_object=None, specific_index=None):
    """source_object: the object the layout being built actually belongs to
    (e.g. 'Account'), used as a fallback -- see determine_effective_target_object
    for why the real comparison basis is usually derived from the data
    itself, not this raw input.

    specific_index: optional (element_type, api_name, veeva_object) -> entry
    lookup, from load_registry. When present and source_object is known,
    tried FIRST -- catches fields that mean something genuinely different
    per object (e.g. 'Name' = Address line 1 on Address_vod__c, but Account
    Name on Account) that the generic index alone would get wrong."""
    specific_index = specific_index or {}
    plausible_target_objects = determine_plausible_target_objects(
        raw_elements, registry_index, specific_index, source_object, source_object)
    plausible_lower = {o.lower() for o in plausible_target_objects}

    classified = []
    for el in raw_elements:
        if el["element_type"].startswith("_"):
            classified.append({**el, "registry_match": None, "action": "informational_only"})
            continue

        # Empty-section exceptions: emit a synthetic Field element for the
        # known implicit field, in ADDITION to classifying the shell, so
        # Generation has real content to build. Never invented — only fires
        # for sections in the explicit exception list above.
        if el["element_type"] == "Section (empty in metadata)":
            exc = EMPTY_SECTION_RENDER_EXCEPTIONS.get(el["layout_section"])
            if exc:
                classified.append({
                    "layout_section": el["layout_section"],
                    "element_type": "Field",
                    "api_name": exc["field"],
                    "behavior": exc["behavior"],
                    "column_index": 1,
                    "registry_match": True,
                    "classification": {"canonical": "Direct", "original": "Direct"},
                    "target": {"api_name": exc["field"], "object": "Account", "confidence": "High"},
                    "action": "auto_generate",
                    "basis": exc["basis"],
                })

        key = registry_key(el["element_type"], el["api_name"], el["layout_section"])
        reg_entry = None
        # Try the object-specific entry FIRST, when we know which object
        # this layout belongs to -- catches fields repurposed with a
        # different meaning per object (e.g. Name on Address_vod__c).
        if source_object and el["element_type"] not in SECTION_SCOPED_TYPES:
            specific_key = (el["element_type"], el["api_name"], source_object)
            reg_entry = specific_index.get(specific_key)
        if reg_entry is None:
            reg_entry = registry_index.get(key)

        if reg_entry:
            action = reg_entry["action"]
            # Registry entries with Low/Medium confidence still require sign-off
            # even if the classification itself says Direct/Map.
            if reg_entry["target"]["confidence"] in ("Low", "Medium") and action == "auto_generate":
                action = "flag_manual_review"

            # Cross-object safety check: only meaningful for Field/Related
            # Cross-object safety check: ONLY meaningful for Fields.
            # Related Lists are DIFFERENT -- their whole purpose is to show
            # records from another/child object, so a different target
            # object there is correct and expected, not a problem. (Caught
            # this exact false-positive during testing: the check initially
            # also applied to Related List, incorrectly flagging entries
            # like Address_vod__c -> ContactPointAddresses -- a real,
            # correct object difference, not a placement error.)
            target_object = reg_entry["target"].get("object")
            basis_suffix = ""
            # Universal standard fields (Name, CreatedById, etc.) exist
            # independently on almost every Salesforce object. Confirmed a
            # real bug: our registry stores ONE answer per field name
            # globally, so "Name" got recorded once against Account (true
            # for the Account/HCO layout) and then wrongly reused for
            # completely unrelated layouts like Address -- incorrectly
            # blocking a field that was always fine, for the wrong reason.
            # These field names are exempt from the cross-object check:
            # their real target is always "whatever object this layout
            # belongs to," never a fixed value from wherever they were
            # first entered into the registry.
            UNIVERSAL_FIELDS = {"Name", "Id", "OwnerId", "CreatedById", "CreatedDate",
                                "LastModifiedById", "LastModifiedDate", "SystemModstamp", "IsDeleted"}
            if (plausible_lower and target_object and el["element_type"] == "Field"
                    and el["api_name"] not in UNIVERSAL_FIELDS
                    and target_object.lower() not in plausible_lower):
                action = "flag_decision_needed"
                plausible_str = ", ".join(sorted(plausible_target_objects))
                basis_suffix = (f" [BLOCKED: target object '{target_object}' is not among this layout's "
                               f"plausible LSC object(s) ({plausible_str}) -- a Field cannot be placed "
                               f"directly on a layout of a different object. Needs a related list, "
                               f"a different page, or explicit scoping decision, not a guess.]")

            classified.append({
                **el,
                "registry_match": True,
                "classification": reg_entry["classification"],
                "target": reg_entry["target"],
                "action": action,
                "basis": reg_entry["basis"] + basis_suffix,
                # Carry through display metadata when the registry has it
                # (currently only entries converted from the Excel mapping
                # files have this -- the original hand-built HCO entries
                # don't, so these will be None for those, which is honest:
                # we genuinely don't have that data for them).
                "veeva_label": reg_entry.get("veeva_label"),
                "veeva_datatype": reg_entry.get("veeva_datatype"),
                "veeva_required": reg_entry.get("veeva_required"),
                # Which Veeva object this SPECIFIC answer actually came
                # from -- lets a reviewer directly verify the tool picked
                # the right context (e.g. confirms this is the
                # Address_vod__c-specific answer for a field, not an
                # accidental cross-object mixup from a same-named field
                # on a different object). Falls back to source_object (the
                # object this classify_elements call is actually for) when
                # the matched registry entry never tracked it explicitly --
                # honest, not a guess: we structurally know the object
                # regardless of whether the registry entry recorded it.
                "veeva_object": reg_entry.get("veeva_object") or source_object,
            })
        else:
            canon, basis = structural_default_classification(el)
            classified.append({
                **el,
                "registry_match": False,
                "classification": {"canonical": canon, "original": canon},
                "target": {"api_name": None, "object": None, "confidence": "N/A"},
                "action": ACTION_MAP.get(canon, "flag_decision_needed") if canon != "Decision Needed"
                          else "flag_no_registry_entry",
                "basis": basis,
            })
    return classified


def link_duplicates(classified):
    """Same (element_type, api_name) appearing in >1 section = same
    underlying element in two locations. Cross-link so Generation builds
    both, and enforce that both locations get the SAME action — a rule
    should never apply to one placement and not the other."""
    groups = defaultdict(list)
    for i, e in enumerate(classified):
        if e["api_name"] in (None, "-", ""):
            continue
        groups[(e["element_type"], e["api_name"])].append(i)

    inconsistencies = []
    for key, idxs in groups.items():
        if len(idxs) <= 1:
            continue
        actions = {classified[i]["action"] for i in idxs}
        if len(actions) > 1:
            inconsistencies.append({
                "element": key,
                "locations": [classified[i]["layout_section"] for i in idxs],
                "conflicting_actions": list(actions),
            })
        for i in idxs:
            classified[i]["also_appears_in_sections"] = [
                classified[j]["layout_section"] for j in idxs if j != i
            ]
    return inconsistencies


def apply_org_verification(classified, describe_path):
    """Optional post-classification step: checks Field elements against a
    REAL org's describe() output. Two cases, since they need different
    handling:

    1. Element already has a registry-proposed target (flag_manual_review,
       Medium/Low confidence) -> verify that exact proposed target against
       the org; upgrade to auto_generate if confirmed, otherwise attach
       real candidate suggestions.
    2. Element has NO registry entry at all (flag_no_registry_entry) -> no
       target to check yet, so first try the Veeva field's own name as a
       same-name guess (the pattern that worked for most of HCO), verify
       that; if it doesn't exist, fuzzy-search the org's real fields by
       label instead of leaving it a blank guess.

    Does nothing to Rebuild/Retire/Decision-Needed items -- those are
    genuine judgment calls, not lookups, and org data can't resolve them.
    Only runs when a --describe file is provided; with no describe data,
    behavior is byte-for-byte identical to before this feature existed.
    """
    from org_verifier import load_describe, verify_exact_field, find_candidate_fields, enrich_registry_entry, verify_exact_related_list, find_candidate_related_lists

    describe_data = load_describe(describe_path)
    verified_count = 0
    candidate_count = 0

    for e in classified:
        if e["element_type"] not in ("Field", "Related List"):
            continue
        if e["action"] not in ("flag_manual_review", "flag_no_registry_entry"):
            continue

        if e["element_type"] == "Related List":
            related_obj = e.get("related_source_object") or e["api_name"].split(".")[0]
            exists, info = verify_exact_related_list(describe_data, related_obj)
            if exists:
                e["target"] = {"api_name": info.get("childSObject"), "object": info.get("childSObject"), "confidence": "High"}
                e["org_verified"] = True
                e["action"] = "auto_generate"
                e["basis"] = (e.get("basis", "") +
                              f" [Org-verified: child object '{info.get('childSObject')}' confirmed "
                              f"to exist as a related list target.]")
                verified_count += 1
            else:
                candidates = find_candidate_related_lists(describe_data, related_obj)
                e["org_verified"] = False
                e["suggested_candidates"] = candidates
                if candidates:
                    candidate_count += 1
                    e["basis"] = (e.get("basis", "") +
                                  f" [Org check: no exact child object match. "
                                  f"{len(candidates)} similar real object(s) found -- review candidates.]")
                else:
                    e["basis"] = (e.get("basis", "") +
                                  " [Org check: no exact or similar child object found -- "
                                  "this relationship likely doesn't exist in the target org's data model yet.]")
            continue

        if e["action"] == "flag_manual_review" and e.get("target", {}).get("api_name"):
            enriched = enrich_registry_entry(e, describe_data)
            e.update(enriched)
            if e.get("org_verified"):
                verified_count += 1
            elif e.get("suggested_candidates"):
                candidate_count += 1
            continue

        # flag_no_registry_entry: no target proposed yet at all.
        same_name_exists, info = verify_exact_field(describe_data, e["api_name"])
        if same_name_exists:
            e["target"] = {"api_name": e["api_name"], "object": "Account", "confidence": "High"}
            e["org_verified"] = True
            e["action"] = "auto_generate"
            e["basis"] = (e.get("basis", "") +
                          f" [Org-verified: '{e['api_name']}' confirmed to exist in target "
                          f"org as-is, same name as Veeva.]")
            verified_count += 1
        else:
            label_guess = e["api_name"].replace("_vod__c", "").replace("__c", "").replace("_", " ")
            candidates = find_candidate_fields(describe_data, label_guess, e["api_name"])
            e["org_verified"] = False
            e["suggested_candidates"] = candidates
            if candidates:
                candidate_count += 1
                e["basis"] = (e.get("basis", "") +
                              f" [Org check: not found by same name. "
                              f"{len(candidates)} similar real field(s) found -- review candidates.]")
            else:
                e["basis"] = (e.get("basis", "") +
                              " [Org check: not found by same name, and no similar field "
                              "found either -- likely doesn't exist in the target org yet.]")

    return {"org_verified_count": verified_count, "candidates_found_count": candidate_count}


def run(xml_path, registry_path, source_object, source_layout, describe_path=None):
    raw_elements = extract_all(xml_path)
    registry_index, registry_conflicts, specific_index = load_registry(registry_path)
    classified = classify_elements(raw_elements, registry_index, source_object, specific_index)
    inconsistencies = link_duplicates(classified)

    org_verification_summary = None
    if describe_path:
        org_verification_summary = apply_org_verification(classified, describe_path)

    from collections import Counter
    action_counts = Counter(e["action"] for e in classified)

    result = {
        "source_object": source_object,
        "source_layout": source_layout,
        "total_elements": len(classified),
        "registry_matched": sum(1 for e in classified if e.get("registry_match")),
        "registry_unmatched": sum(1 for e in classified if e.get("registry_match") is False),
        "action_summary": dict(action_counts),
        "registry_load_conflicts": {
            "status": "OK" if not registry_conflicts else "CONFLICTS_FOUND",
            "conflicts": registry_conflicts,
        },
        "duplicate_consistency_check": {
            "status": "OK" if not inconsistencies else "CONFLICTS_FOUND",
            "conflicts": inconsistencies,
        },
        "elements": classified,
    }
    if org_verification_summary:
        result["org_verification_summary"] = org_verification_summary
    return result


def main():
    ap = argparse.ArgumentParser(description="Layer 3: classify a Veeva layout's elements for LSC migration.")
    ap.add_argument("--object", required=True, help="Salesforce object, e.g. Account")
    ap.add_argument("--layout", required=True, help="Layout name, e.g. SP_Admin_Layout_HCO")
    ap.add_argument("--xml", required=True, help="Path to Veeva layout-meta.xml")
    ap.add_argument("--registry", default=None, help="Path to mapping_registry.json")
    ap.add_argument("--describe", default=None,
                    help="Optional: path to a live org's sobject describe JSON "
                         "(from 'sf sobject describe --json') to verify/upgrade "
                         "flagged fields against real org data")
    ap.add_argument("--out", default=None, help="Output JSON path (default: stdout)")
    args = ap.parse_args()

    result = run(args.xml, args.registry, args.object, args.layout, args.describe)

    output = json.dumps(result, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Classified {result['total_elements']} elements "
              f"({result['registry_matched']} matched / {result['registry_unmatched']} unmatched) "
              f"-> {args.out}")
        print("Action summary:", result["action_summary"])
        print("Duplicate consistency check:", result["duplicate_consistency_check"]["status"])
        if "org_verification_summary" in result:
            print("Org verification:", result["org_verification_summary"])
    else:
        print(output)


if __name__ == "__main__":
    main()
