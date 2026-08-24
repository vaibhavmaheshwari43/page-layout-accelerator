#!/usr/bin/env python3
"""
Veeva -> LSC Page Layout Accelerator — Layer 1: Extraction (pure parsing only)

Parses a Veeva Salesforce Layout metadata XML into a normalized, structural,
object-agnostic list of every raw element on the layout (fields, buttons,
custom links, embedded pages, empty sections, related lists, quick actions,
settings).

This layer does NOT classify anything and does NOT read the Mapping
Registry — that judgment work belongs to Layer 3 (rules_engine.py). Keeping
this file to parsing-only means the same extraction logic works unchanged
for any object/layout (HCO, HCP, ...), and rule changes never require
touching the parser.

Usage (as a script — raw output only, no classification):
    python extractor.py --xml path/to/layout.xml --out raw_elements.json

Usage (as a library, from rules_engine.py):
    from extractor import extract_all
    raw_elements = extract_all(xml_path)
"""

import argparse
import json
import xml.etree.ElementTree as ET

NS = {"sf": "http://soap.sforce.com/2006/04/metadata"}


def tag(el, name):
    """Namespaced find-text helper."""
    found = el.find(f"sf:{name}", NS)
    return found.text if found is not None else None


def tags(el, name):
    """Namespaced find-all-text helper."""
    return [e.text for e in el.findall(f"sf:{name}", NS)]


def extract_page_level_buttons(root):
    elements = []
    for name in tags(root, "customButtons"):
        elements.append({
            "layout_section": "Buttons (page-level)",
            "element_type": "Custom Button",
            "api_name": name,
        })
    return elements


def extract_field_sections(root):
    """Handles: normal field sections, CustomLinks sections, embedded <page>
    components, and empty placeholder sections."""
    elements = []
    for section in root.findall("sf:layoutSections", NS):
        label = tag(section, "label")
        style = tag(section, "style")
        columns = section.findall("sf:layoutColumns", NS)

        items = []
        for col_idx, col in enumerate(columns, start=1):
            for li in col.findall("sf:layoutItems", NS):
                items.append((col_idx, li))

        if not items:
            elements.append({
                "layout_section": label,
                "element_type": "Section (empty in metadata)",
                "api_name": "-",
                "style": style,
            })
            continue

        for col_idx, item in items:
            field = tag(item, "field")
            custom_link = tag(item, "customLink")
            page = tag(item, "page")
            behavior = tag(item, "behavior")

            if field:
                elements.append({
                    "layout_section": label,
                    "element_type": "Field",
                    "api_name": field,
                    "behavior": behavior or "-",
                    "column_index": col_idx,
                })
            elif custom_link:
                elements.append({
                    "layout_section": label,
                    "element_type": "Custom Link",
                    "api_name": custom_link,
                    "column_index": col_idx,
                })
            elif page:
                elements.append({
                    "layout_section": label,
                    "element_type": "Embedded Lightning Page",
                    "api_name": page,
                    "column_index": col_idx,
                })
            else:
                elements.append({
                    "layout_section": label,
                    "element_type": "Unknown layoutItem (needs manual inspection)",
                    "api_name": "-",
                    "column_index": col_idx,
                })
    return elements


def extract_related_lists(root):
    elements = []
    for rl in root.findall("sf:relatedLists", NS):
        rl_name = tag(rl, "relatedList")
        elements.append({
            "layout_section": "Related Lists",
            "element_type": "Related List",
            "api_name": rl_name,
            "related_source_object": rl_name.split(".")[0] if rl_name and "." in rl_name else None,
        })
    return elements


def extract_actions(root):
    """Quick actions declared in the XML. Standard buttons (Delete,
    ChangeOwnerOne, PrintableView, etc.) are usually NOT declared explicitly
    in Veeva source XML — they render by Salesforce default — so they can't
    be structurally extracted from this file alone. Flagged as a known
    extraction gap rather than silently assumed absent."""
    elements = []
    for qa in root.findall("sf:quickActionList/sf:quickActionListItems", NS):
        name = tag(qa, "quickActionName")
        elements.append({
            "layout_section": "Actions (Highlights Panel)",
            "element_type": "Quick Action",
            "api_name": name,
        })
    elements.append({
        "layout_section": "Actions (Highlights Panel)",
        "element_type": "_extraction_gap_note",
        "api_name": "-",
        "note": (
            "Standard action buttons (Delete, ChangeOwnerOne, ChangeRecordType, "
            "PrintableView, etc.) are implicit Salesforce defaults, not present as "
            "explicit XML nodes in Veeva source layouts. They cannot be structurally "
            "extracted from this file — cross-check against the Decision Matrix baseline "
            "or the target org's default action set."
        ),
    })
    return elements


def extract_mini_layout(root):
    elements = []
    mini = root.find("sf:miniLayout", NS)
    if mini is not None:
        for f in tags(mini, "fields"):
            elements.append({
                "layout_section": "Mini/Compact Layout",
                "element_type": "Field",
                "api_name": f,
            })
    return elements


def extract_settings(root):
    elements = []
    show_hp = tag(root, "showHighlightsPanel")
    if show_hp is not None:
        elements.append({
            "layout_section": "Layout Settings",
            "element_type": "Setting",
            "api_name": f"showHighlightsPanel = {show_hp}",
        })
    summary = root.find("sf:summaryLayout", NS)
    if summary is not None:
        master_label = tag(summary, "masterLabel")
        elements.append({
            "layout_section": "Layout Settings",
            "element_type": "Setting",
            "api_name": f"summaryLayout masterLabel (hardcoded ID: {master_label})",
        })
    return elements


def extract_all(xml_path):
    """Pure Layer-1 extraction. Returns a flat list of raw element dicts —
    no classification, no registry lookup, no defaults applied."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    elements = []
    elements += extract_page_level_buttons(root)
    elements += extract_field_sections(root)
    elements += extract_related_lists(root)
    elements += extract_actions(root)
    elements += extract_mini_layout(root)
    elements += extract_settings(root)
    return elements


def main():
    ap = argparse.ArgumentParser(description="Extract raw elements from a Veeva layout XML (no classification).")
    ap.add_argument("--xml", required=True, help="Path to Veeva layout-meta.xml")
    ap.add_argument("--out", default=None, help="Output JSON path (default: stdout)")
    args = ap.parse_args()

    raw_elements = extract_all(args.xml)
    output = json.dumps({"total_raw_elements": len(raw_elements), "elements": raw_elements}, indent=2)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Wrote {len(raw_elements)} raw elements to {args.out}")
    else:
        print(output)


if __name__ == "__main__":
    main()
