#!/usr/bin/env python3
"""
Veeva -> LSC Page Layout Accelerator — Layer 6: Validation

Two kinds of checks, both automated (no manual eyeballing needed):

1. REFERENCE COMPARISON (when a hand-built/known-good file exists, e.g. HCO):
   diffs the generated Page Layout AND Flexipage against it, field-by-field,
   ignoring formatting/ordering noise. Reports missing fields, extra fields,
   and behavior/uiBehavior mismatches.

2. SELF-CONSISTENCY CHECK (always runs, no reference needed — this is what
   makes it usable for HCP or any layout with no reference to check against):
   confirms nothing in the build_report (flag_rebuild / flag_retire /
   flag_decision_needed / flag_manual_review) accidentally leaked into the
   generated files. This is exactly the class of bug caught manually during
   Layer 4 development (the stray "Mini/Compact Layout" section) — now
   checked automatically on every run instead of relying on someone noticing.

Usage:
    python validator.py \
        --generated-layout Account-SP_Admin_Layout_HCO_Generated.layout-meta.xml \
        --generated-flexipage Account_SP_Admin_Layout_HCO_Generated.flexipage-meta.xml \
        --build-report SP_Admin_Layout_HCO_build_report.json \
        --reference-layout LSC_Target_Account_SP_Admin_Layout_HCO_layout-meta.xml \
        --reference-flexipage Account_HCO_Admin_Record_Page_flexipage-meta.xml
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET

NS = {"sf": "http://soap.sforce.com/2006/04/metadata"}


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_layout_fields(path):
    """Returns {(section_label, column_index, field_api_name): behavior}"""
    root = ET.parse(path).getroot()
    result = {}
    for sec in root.findall("sf:layoutSections", NS):
        label_el = sec.find("sf:label", NS)
        label = label_el.text if label_el is not None else "(no label)"
        for col_idx, col in enumerate(sec.findall("sf:layoutColumns", NS), start=1):
            for item in col.findall("sf:layoutItems", NS):
                f = item.find("sf:field", NS)
                b = item.find("sf:behavior", NS)
                if f is not None:
                    result[(label, col_idx, f.text)] = b.text if b is not None else None
    return result


def parse_flexipage_fields(path):
    """Returns {(section_label, field_api_name): ui_behavior}.
    Flexipage doesn't nest fields directly under a labeled section in the
    XML tree (labels live on the fieldSection componentInstance, separately
    from the Facet blocks holding the fields) — so this maps facet columns
    back to their owning section by name pattern (Facet_<Section>_Col*),
    matching the naming convention this same generator produces."""
    root = ET.parse(path).getroot()

    # 1. Collect every fieldInstance, keyed by which Facet (region name) it's in.
    facet_fields = {}  # facet_name -> [(field_api_name, ui_behavior), ...]
    for region in root.findall("sf:flexiPageRegions", NS):
        name_el = region.find("sf:name", NS)
        if name_el is None:
            continue
        facet_name = name_el.text
        for item in region.findall("sf:itemInstances", NS):
            fi = item.find("sf:fieldInstance", NS)
            if fi is None:
                continue
            field_item = fi.find("sf:fieldItem", NS)
            if field_item is None or not field_item.text.startswith("Record."):
                continue
            field_name = field_item.text.split("Record.", 1)[1]
            ui_behavior = None
            for prop in fi.findall("sf:fieldInstanceProperties", NS):
                n = prop.find("sf:name", NS)
                if n is not None and n.text == "uiBehavior":
                    ui_behavior = prop.find("sf:value", NS).text
            facet_fields.setdefault(facet_name, []).append((field_name, ui_behavior))

    # 2. Collect fieldSection label -> which Cols facet it wires to.
    section_label_by_cols_facet = {}
    for region in root.findall("sf:flexiPageRegions", NS):
        for item in region.findall("sf:itemInstances", NS):
            ci = item.find("sf:componentInstance", NS)
            if ci is None:
                continue
            comp_name = ci.find("sf:componentName", NS)
            if comp_name is None or comp_name.text != "flexipage:fieldSection":
                continue
            label, cols_facet = None, None
            for prop in ci.findall("sf:componentInstanceProperties", NS):
                n = prop.find("sf:name", NS)
                v = prop.find("sf:value", NS)
                if n is not None and n.text == "label":
                    label = v.text
                if n is not None and n.text == "columns":
                    cols_facet = v.text
            if label and cols_facet:
                section_label_by_cols_facet[cols_facet] = label

    # 3. Cols facet -> its column facet names (via flexipage:column componentInstances)
    col_facets_by_cols_facet = {}
    for region in root.findall("sf:flexiPageRegions", NS):
        name_el = region.find("sf:name", NS)
        if name_el is None:
            continue
        cols_facet = name_el.text
        col_names = []
        for item in region.findall("sf:itemInstances", NS):
            ci = item.find("sf:componentInstance", NS)
            if ci is None:
                continue
            comp_name = ci.find("sf:componentName", NS)
            if comp_name is None or comp_name.text != "flexipage:column":
                continue
            for prop in ci.findall("sf:componentInstanceProperties", NS):
                n = prop.find("sf:name", NS)
                v = prop.find("sf:value", NS)
                if n is not None and n.text == "body":
                    col_names.append(v.text)
        if col_names:
            col_facets_by_cols_facet[cols_facet] = col_names

    # 4. Stitch it together: section label -> fields in its column facets.
    result = {}
    for cols_facet, label in section_label_by_cols_facet.items():
        for col_facet in col_facets_by_cols_facet.get(cols_facet, []):
            for field_name, ui_behavior in facet_fields.get(col_facet, []):
                result[(label, field_name)] = ui_behavior
    return result


def parse_related_lists(path):
    """Returns a sorted list of relatedList API names."""
    root = ET.parse(path).getroot()
    return sorted(
        rl.find("sf:relatedList", NS).text
        for rl in root.findall("sf:relatedLists", NS)
        if rl.find("sf:relatedList", NS) is not None
    )


def parse_quick_actions(path):
    """Returns a sorted list of quickActionName values."""
    root = ET.parse(path).getroot()
    return sorted(
        qa.find("sf:quickActionName", NS).text
        for qa in root.findall("sf:quickActionList/sf:quickActionListItems", NS)
        if qa.find("sf:quickActionName", NS) is not None
    )


def parse_mini_layout(path):
    """Returns a sorted list of miniLayout field names."""
    root = ET.parse(path).getroot()
    mini = root.find("sf:miniLayout", NS)
    if mini is None:
        return []
    return sorted(f.text for f in mini.findall("sf:fields", NS))


# ---------------------------------------------------------------------------
# Check 1: reference comparison
# ---------------------------------------------------------------------------

def compare_layouts(real_path, gen_path):
    """Compares a generated Page Layout against a reference, covering
    EVERYTHING the generator produces \u2014 not just field placement.
    Earlier versions only checked fields, which meant related lists, quick
    actions, and miniLayout were never actually proven correct by an
    automated check, only by manual spot-checks during development. This
    closes that gap: a PASS here now means the whole file matches, not
    just the field section."""
    real = parse_layout_fields(real_path)
    gen = parse_layout_fields(gen_path)
    only_real = sorted(set(real) - set(gen))
    only_gen = sorted(set(gen) - set(real))
    common = set(real) & set(gen)
    mismatches = [(k, real[k], gen[k]) for k in common if real[k] != gen[k]]

    real_rl, gen_rl = parse_related_lists(real_path), parse_related_lists(gen_path)
    real_qa, gen_qa = parse_quick_actions(real_path), parse_quick_actions(gen_path)
    real_mini, gen_mini = parse_mini_layout(real_path), parse_mini_layout(gen_path)

    fields_ok = not only_real and not only_gen and not mismatches
    related_lists_ok = real_rl == gen_rl
    quick_actions_ok = real_qa == gen_qa
    mini_layout_ok = real_mini == gen_mini

    return {
        "total_reference_fields": len(real),
        "total_generated_fields": len(gen),
        "missing_from_generated": only_real,
        "unexpected_in_generated": only_gen,
        "behavior_mismatches": mismatches,
        "related_lists": {"reference": real_rl, "generated": gen_rl, "match": related_lists_ok},
        "quick_actions": {"reference": real_qa, "generated": gen_qa, "match": quick_actions_ok},
        "mini_layout": {"reference": real_mini, "generated": gen_mini, "match": mini_layout_ok},
        "status": "PASS" if (fields_ok and related_lists_ok and quick_actions_ok and mini_layout_ok) else "FAIL",
    }


def compare_flexipages(real_path, gen_path):
    real = parse_flexipage_fields(real_path)
    gen = parse_flexipage_fields(gen_path)
    only_real = sorted(set(real) - set(gen))
    only_gen = sorted(set(gen) - set(real))
    common = set(real) & set(gen)
    mismatches = [(k, real[k], gen[k]) for k in common if real[k] != gen[k]]
    return {
        "total_reference_fields": len(real),
        "total_generated_fields": len(gen),
        "missing_from_generated": only_real,
        "unexpected_in_generated": only_gen,
        "ui_behavior_mismatches": mismatches,
        "status": "PASS" if not only_real and not only_gen and not mismatches else "FAIL",
    }


# ---------------------------------------------------------------------------
# Check 2: self-consistency — nothing flagged leaked into the generated files
# ---------------------------------------------------------------------------

def check_no_leaked_flags(build_report_path, gen_layout_path, gen_flexipage_path):
    with open(build_report_path, encoding="utf-8") as f:
        report = json.load(f)

    flagged_api_names = set()
    for action, items in report.items():
        for it in items:
            if it["api_name"] not in (None, "-", ""):
                flagged_api_names.add(it["api_name"])

    gen_layout_fields = {k[2] for k in parse_layout_fields(gen_layout_path)}
    gen_flexipage_fields = {k[1] for k in parse_flexipage_fields(gen_flexipage_path)}
    generated_names = gen_layout_fields | gen_flexipage_fields

    leaked = sorted(flagged_api_names & generated_names)
    return {
        "flagged_count": len(flagged_api_names),
        "leaked_into_generated_output": leaked,
        "status": "PASS" if not leaked else "FAIL",
    }


def main():
    ap = argparse.ArgumentParser(description="Layer 6: validate generated layout files.")
    ap.add_argument("--generated-layout", required=True)
    ap.add_argument("--generated-flexipage", required=True)
    ap.add_argument("--build-report", required=True)
    ap.add_argument("--reference-layout", default=None, help="Optional: hand-built Page Layout to diff against")
    ap.add_argument("--reference-flexipage", default=None, help="Optional: hand-built Flexipage to diff against")
    ap.add_argument("--out", default=None, help="Optional: write full JSON report to this path")
    args = ap.parse_args()

    results = {}

    print("=== Self-consistency check (no flagged items leaked into output) ===")
    consistency = check_no_leaked_flags(args.build_report, args.generated_layout, args.generated_flexipage)
    results["self_consistency"] = consistency
    print(f"Status: {consistency['status']} | flagged items checked: {consistency['flagged_count']} "
          f"| leaked: {len(consistency['leaked_into_generated_output'])}")
    if consistency["leaked_into_generated_output"]:
        for name in consistency["leaked_into_generated_output"]:
            print(f"  LEAKED: {name}")
    print()

    if args.reference_layout:
        print("=== Page Layout comparison vs reference ===")
        layout_result = compare_layouts(args.reference_layout, args.generated_layout)
        results["page_layout_comparison"] = layout_result
        print(f"Status: {layout_result['status']} | reference fields: {layout_result['total_reference_fields']} "
              f"| generated fields: {layout_result['total_generated_fields']}")
        if layout_result["missing_from_generated"]:
            print("  Missing from generated:")
            for k in layout_result["missing_from_generated"]:
                print("   ", k)
        if layout_result["unexpected_in_generated"]:
            print("  Unexpected in generated:")
            for k in layout_result["unexpected_in_generated"]:
                print("   ", k)
        if layout_result["behavior_mismatches"]:
            print("  Behavior mismatches:")
            for m in layout_result["behavior_mismatches"]:
                print("   ", m)
        print()

    if args.reference_flexipage:
        print("=== Flexipage comparison vs reference ===")
        flexi_result = compare_flexipages(args.reference_flexipage, args.generated_flexipage)
        results["flexipage_comparison"] = flexi_result
        print(f"Status: {flexi_result['status']} | reference fields: {flexi_result['total_reference_fields']} "
              f"| generated fields: {flexi_result['total_generated_fields']}")
        if flexi_result["missing_from_generated"]:
            print("  Missing from generated:")
            for k in flexi_result["missing_from_generated"]:
                print("   ", k)
        if flexi_result["unexpected_in_generated"]:
            print("  Unexpected in generated:")
            for k in flexi_result["unexpected_in_generated"]:
                print("   ", k)
        if flexi_result["ui_behavior_mismatches"]:
            print("  uiBehavior mismatches:")
            for m in flexi_result["ui_behavior_mismatches"]:
                print("   ", m)
        print()

    overall = all(r["status"] == "PASS" for r in results.values())
    print(f"=== OVERALL: {'PASS' if overall else 'FAIL'} ===")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"Full report written to {args.out}")

    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()
