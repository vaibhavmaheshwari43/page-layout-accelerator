#!/usr/bin/env python3
"""
Veeva -> LSC Page Layout Accelerator — Layout Mapping Converter

Converts the layout-mapping Excel files (produced by the mapping
accelerator, organized per real Veeva layout name -- e.g. one sheet per
layout like 'SP_Admin_Layout_HCO') into our mapping_registry.json format.

Handles two real, confirmed differences between files:
  - Column schema differs slightly: the Account file has an explicit
    'Veeva Object' column; the Address/Call2 files don't (the object is
    implicit from which file/sheet the row came from). Detected by header
    name, not fixed column position, so both work.
  - An extra status value ('PARTIALLY_MAPPED') not seen in earlier files.

Status -> classification/action translation (confirmed against real,
already-known-correct HCO data before trusting this broadly):
  MAPPED              -> Map, auto_generate, High confidence
  GAP_CUSTOM_FIELD     -> Map, auto_generate, Medium confidence
                          (the target name is correct, but the field needs
                          to be created in the org first -- same situation
                          we already lived through manually for HCO)
  PARTIALLY_MAPPED     -> Decision Needed (genuinely ambiguous, needs a
                          human call, not a guess)
  REFERENCE            -> Retire (candidate for removal, but still needs
                          explicit sign-off -- never silently dropped)
  UNKNOWN               -> Decision Needed, no target (matches the exact
                          fields we independently flagged as unresolved)

Deduplication: entries are collected across ALL layouts/sheets/files, since
our registry is keyed on (element_type, api_name), not per-layout. If the
same field appears with different status/target across layouts, that's a
real conflict -- flagged, not silently overwritten.
"""

import argparse
import json
import openpyxl
from collections import defaultdict

STATUS_MAP = {
    "MAPPED": ("Map", "auto_generate", "High"),
    "GAP_CUSTOM_FIELD": ("Map", "auto_generate", "Medium"),
    "PARTIALLY_MAPPED": ("Decision Needed", "flag_decision_needed", "N/A"),
    "REFERENCE": ("Retire", "flag_retire", "N/A"),
    "UNKNOWN": ("Decision Needed", "flag_decision_needed", "N/A"),
}

SKIP_SHEETS = {"Summary", "Suggested LSC Layout", "LSC Existing Layouts"}


def find_header_indices(header_row):
    """Maps column header names to their index, so this works regardless of
    exact column order/count differing between files."""
    idx = {}
    for i, h in enumerate(header_row):
        if h:
            idx[str(h).strip()] = i
    return idx


def convert_workbook(path, default_veeva_object=None):
    """Returns a list of raw parsed rows: (layout_name, veeva_field,
    lsc_object, lsc_field, status, action_note)."""
    wb = openpyxl.load_workbook(path, data_only=True)
    rows_out = []

    for sheet_name in wb.sheetnames:
        if sheet_name in SKIP_SHEETS:
            continue
        ws = wb[sheet_name]
        all_rows = list(ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=True))
        if not all_rows:
            continue
        header = find_header_indices(all_rows[0])

        # Required columns -- if these aren't present, this sheet doesn't
        # match the expected schema, skip it rather than guess.
        if "Veeva Field" not in header or "Status" not in header:
            continue

        veeva_obj_idx = header.get("Veeva Object")
        veeva_field_idx = header["Veeva Field"]
        label_idx = header.get("Label")
        type_idx = header.get("Type")
        req_idx = header.get("Req")
        lsc_obj_idx = header.get("LSC Object")
        lsc_field_idx = header.get("LSC Field")
        status_idx = header["Status"]
        action_idx = header.get("Action")

        for row in all_rows[1:]:
            if row is None or veeva_field_idx >= len(row):
                continue
            veeva_field = row[veeva_field_idx]
            if not veeva_field:
                continue  # section header row or blank
            status = row[status_idx] if status_idx < len(row) else None
            if not status or status not in STATUS_MAP:
                continue  # not a real data row (e.g. a section divider)

            veeva_object = row[veeva_obj_idx] if veeva_obj_idx is not None and veeva_obj_idx < len(row) else default_veeva_object
            label = row[label_idx] if label_idx is not None and label_idx < len(row) else None
            datatype = row[type_idx] if type_idx is not None and type_idx < len(row) else None
            required = row[req_idx] if req_idx is not None and req_idx < len(row) else None
            lsc_object = row[lsc_obj_idx] if lsc_obj_idx is not None and lsc_obj_idx < len(row) else None
            lsc_field = row[lsc_field_idx] if lsc_field_idx is not None and lsc_field_idx < len(row) else None
            action_note = row[action_idx] if action_idx is not None and action_idx < len(row) else ""

            rows_out.append({
                "layout": sheet_name,
                "veeva_object": veeva_object,
                "veeva_field": veeva_field,
                "label": label,
                "datatype": datatype,
                "required": required,
                "lsc_object": lsc_object,
                "lsc_field": lsc_field,
                "status": status,
                "note": action_note or "",
            })
    return rows_out


def build_registry_entries(all_rows):
    """Deduplicates across all layouts by (element_type, api_name) --
    matching our registry's actual matching key. Flags real conflicts
    instead of silently picking one when the same field shows different
    answers across layouts."""
    grouped = defaultdict(list)
    for r in all_rows:
        # Group by (field, veeva_object) -- NOT just field name. A real bug
        # was found: "Name" means "Account Name" on the Account object, but
        # gets legitimately repurposed to mean "Address line 1" on the
        # Address_vod__c object -- these are genuinely different, correct
        # entries, not a conflict to resolve. Grouping by name alone would
        # collapse them together and silently lose the object-specific one.
        grouped[(r["veeva_field"], r["veeva_object"])].append(r)

    entries = []
    conflicts = []

    for (field_name, veeva_object), occurrences in grouped.items():
        statuses = {o["status"] for o in occurrences}
        targets = {(o["lsc_object"], o["lsc_field"]) for o in occurrences}

        if len(statuses) > 1 or len(targets) > 1:
            conflicts.append({
                "field": field_name,
                "veeva_object": veeva_object,
                "occurrences": [
                    {"layout": o["layout"], "status": o["status"],
                     "lsc_object": o["lsc_object"], "lsc_field": o["lsc_field"]}
                    for o in occurrences
                ],
            })

        # Use the first occurrence as the representative entry even when
        # conflicting -- the conflict itself is what matters, surfaced
        # separately for review, not silently resolved here.
        rep = occurrences[0]
        classification, action, confidence = STATUS_MAP[rep["status"]]

        entries.append({
            "layout_section": rep["layout"],
            "element_type": "Field",
            "api_name": field_name,
            "veeva_object": veeva_object,
            "veeva_label": rep.get("label"),
            "veeva_datatype": rep.get("datatype"),
            "veeva_required": rep.get("required"),
            "classification": {"canonical": classification, "original": rep["status"]},
            "target": {
                "api_name": rep["lsc_field"] if classification in ("Map", "Direct") else None,
                "object": rep["lsc_object"] if classification in ("Map", "Direct") else None,
                "confidence": confidence,
            },
            "action": action,
            "basis": (f"From layout mapping file (status: {rep['status']}). {rep['note']}").strip(),
        })

    return entries, conflicts


def main():
    ap = argparse.ArgumentParser(description="Convert layout-mapping Excel files into mapping_registry.json format.")
    ap.add_argument("--files", nargs="+", required=True, help="Excel file paths")
    ap.add_argument("--default-objects", nargs="*", default=[],
                    help="Default Veeva object per file (for files without an explicit column), "
                         "in the same order as --files, use '-' for files that have the column.")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    all_rows = []
    for i, path in enumerate(args.files):
        default_obj = None
        if i < len(args.default_objects) and args.default_objects[i] != "-":
            default_obj = args.default_objects[i]
        rows = convert_workbook(path, default_obj)
        print(f"{path}: parsed {len(rows)} rows")
        all_rows.extend(rows)

    entries, conflicts = build_registry_entries(all_rows)

    print(f"\nTotal distinct fields: {len(entries)}")
    print(f"Conflicts found (same field, different answer across layouts): {len(conflicts)}")

    from collections import Counter
    action_counts = Counter(e["action"] for e in entries)
    print(f"Action breakdown: {dict(action_counts)}")

    output = {"entries": entries}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"\nWritten to {args.out}")

    if conflicts:
        conflicts_path = args.out.replace(".json", "_conflicts.json")
        with open(conflicts_path, "w", encoding="utf-8") as f:
            json.dump(conflicts, f, indent=2)
        print(f"Conflicts written to {conflicts_path} -- review these before fully trusting the registry.")


if __name__ == "__main__":
    main()
