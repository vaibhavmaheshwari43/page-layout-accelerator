#!/usr/bin/env python3
"""
Veeva -> LSC Page Layout Accelerator — Review UI

A local web app wrapping the existing pipeline (extractor.py, rules_engine.py,
generator.py, validator.py, deploy_prep.py) with a human-in-the-loop review
step, matching the workflow: upload a Veeva layout -> tool proposes a
classification -> a person reviews/edits/comments -> approved decisions get
generated -> generated output gets validated.

This file does NOT duplicate any pipeline logic — it imports the real
functions from the other scripts, so the UI and the command-line tools are
always doing exactly the same thing under the hood.

Run with:
    streamlit run app.py

Requires the other 5 .py files to be in the same folder (same import
requirement rules_engine.py already has on extractor.py).
"""

import json
import os
import tempfile

import pandas as pd
import streamlit as st

from extractor import extract_all
from rules_engine import classify_elements, link_duplicates, load_registry, registry_key
from generator import group_fields_by_section, gen_flexipage, gen_page_layout, build_report
from validator import (
    parse_layout_fields, parse_flexipage_fields, compare_layouts,
    compare_flexipages, check_no_leaked_flags,
)

st.set_page_config(page_title="Page Layout Accelerator", layout="wide")
st.title("Veeva \u2192 LSC Page Layout Accelerator")
st.caption("Extract \u2192 Review & Approve \u2192 Generate \u2192 Validate")

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
if "classified" not in st.session_state:
    st.session_state.classified = None
if "approved_df" not in st.session_state:
    st.session_state.approved_df = None
if "generated" not in st.session_state:
    st.session_state.generated = None

# ---------------------------------------------------------------------------
# Step 0: Inputs
# ---------------------------------------------------------------------------
st.header("1. Input")
col1, col2 = st.columns(2)
with col1:
    object_name = st.text_input("Salesforce Object", value="Account")
    layout_name = st.text_input("Layout Name", value="SP_Admin_Layout_HCO")
with col2:
    veeva_file = st.file_uploader("Veeva layout XML", type=["xml"])
    registry_file = st.file_uploader("Mapping Registry JSON (existing)", type=["json"])

run_col1, run_col2 = st.columns([1, 4])
with run_col1:
    run_clicked = st.button("Run Extraction + Classification", type="primary")

if run_clicked:
    if not veeva_file or not registry_file:
        st.error("Please upload both the Veeva layout XML and the Mapping Registry JSON.")
    else:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xml") as tmp_xml:
            tmp_xml.write(veeva_file.getvalue())
            xml_path = tmp_xml.name
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w") as tmp_reg:
            tmp_reg.write(registry_file.getvalue().decode("utf-8"))
            registry_path = tmp_reg.name

        raw_elements = extract_all(xml_path)
        registry_index, registry_conflicts = load_registry(registry_path)
        classified = classify_elements(raw_elements, registry_index)
        inconsistencies = link_duplicates(classified)

        st.session_state.classified = classified
        st.session_state.registry_conflicts = registry_conflicts
        st.session_state.duplicate_conflicts = inconsistencies
        st.session_state.approved_df = None
        st.session_state.generated = None

        os.unlink(xml_path)
        os.unlink(registry_path)

# ---------------------------------------------------------------------------
# Step 1 result: summary + warnings
# ---------------------------------------------------------------------------
if st.session_state.classified:
    classified = st.session_state.classified
    matched = sum(1 for e in classified if e.get("registry_match"))
    unmatched = sum(1 for e in classified if e.get("registry_match") is False)

    st.success(f"Extracted {len(classified)} elements \u2014 {matched} matched the registry, {unmatched} did not.")

    if st.session_state.registry_conflicts:
        st.warning(f"\u26a0\ufe0f Registry has {len(st.session_state.registry_conflicts)} internal conflict(s) "
                   f"\u2014 same element, disagreeing answers. Fix the registry before trusting these rows.")
        st.json(st.session_state.registry_conflicts)

    if st.session_state.duplicate_conflicts:
        st.warning(f"\u26a0\ufe0f {len(st.session_state.duplicate_conflicts)} element(s) appear in multiple "
                   f"locations with DIFFERENT actions \u2014 review these before approving.")
        st.json(st.session_state.duplicate_conflicts)

    # ---------------------------------------------------------------------
    # Step 2: Review & Approve table
    # ---------------------------------------------------------------------
    st.header("2. Review & Approve")
    st.caption("Edit Action / Target / add a comment for anything that needs a human call. "
               "Nothing is built until you click Approve below.")

    rows = []
    for i, e in enumerate(classified):
        rows.append({
            "idx": i,
            "Section": e["layout_section"],
            "Type": e["element_type"],
            "API Name": e["api_name"],
            "Classification": e.get("classification", {}).get("canonical", "-"),
            "Target": e.get("target", {}).get("api_name") or "-",
            "Confidence": e.get("target", {}).get("confidence", "-"),
            "Action": e.get("action", "-"),
            "Basis": e.get("basis", "-"),
            "Reviewer Comment": "",
        })
    df = pd.DataFrame(rows)

    action_options = ["auto_generate", "flag_manual_review", "flag_rebuild",
                       "flag_retire", "flag_decision_needed", "flag_no_registry_entry",
                       "informational_only"]

    edited_df = st.data_editor(
        df,
        column_config={
            "idx": None,  # hide internal index
            "Action": st.column_config.SelectboxColumn(options=action_options),
            "Basis": st.column_config.TextColumn(width="large"),
            "Reviewer Comment": st.column_config.TextColumn(width="medium"),
        },
        hide_index=True,
        use_container_width=True,
        key="review_editor",
    )

    approve_clicked = st.button("\u2705 Approve reviewed decisions", type="primary")
    if approve_clicked:
        st.session_state.approved_df = edited_df
        st.session_state.generated = None
        st.success("Decisions approved. Scroll down to generate.")

# ---------------------------------------------------------------------------
# Step 3: Generate
# ---------------------------------------------------------------------------
if st.session_state.approved_df is not None:
    st.header("3. Generate")

    approved = st.session_state.approved_df
    # Rebuild the classified element list using the REVIEWER'S edited
    # Action/Target values, not the original proposal — this is the actual
    # "human approval overrides the draft" step.
    reviewed_elements = []
    for i, e in enumerate(st.session_state.classified):
        row = approved.iloc[i]
        e2 = dict(e)
        e2["action"] = row["Action"]
        e2["target"] = {**e.get("target", {}), "api_name": row["Target"] if row["Target"] != "-" else None}
        e2["reviewer_comment"] = row["Reviewer Comment"]
        reviewed_elements.append(e2)

    if st.button("\u2699\ufe0f Generate Flexipage + Page Layout", type="primary"):
        section_order, sections_by_col = group_fields_by_section(reviewed_elements)
        flexipage_xml = gen_flexipage(section_order, sections_by_col, object_name, layout_name)
        layout_xml = gen_page_layout(section_order, sections_by_col, reviewed_elements, object_name, layout_name)
        report = build_report(reviewed_elements)

        st.session_state.generated = {
            "flexipage_xml": flexipage_xml,
            "layout_xml": layout_xml,
            "report": report,
            "reviewed_elements": reviewed_elements,
        }

if st.session_state.generated:
    gen = st.session_state.generated
    st.success("Generated successfully.")

    tab1, tab2, tab3 = st.tabs(["Page Layout XML", "Flexipage XML", "Build Report (not built)"])
    with tab1:
        st.code(gen["layout_xml"], language="xml")
        st.download_button("Download Layout XML", gen["layout_xml"],
                            file_name=f"{object_name}-{layout_name}_Generated.layout-meta.xml")
    with tab2:
        st.code(gen["flexipage_xml"], language="xml")
        st.download_button("Download Flexipage XML", gen["flexipage_xml"],
                            file_name=f"{object_name}_{layout_name}_Generated.flexipage-meta.xml")
    with tab3:
        st.json(gen["report"])

    # -----------------------------------------------------------------
    # Step 4: Validate
    # -----------------------------------------------------------------
    st.header("4. Validate")
    st.caption("Self-consistency check always runs. Reference comparison is optional "
               "(only if you have a known-correct hand-built file to check against, e.g. HCO).")

    # Write generated files to temp paths so the existing validator functions
    # (which parse from disk) can run unmodified.
    with tempfile.NamedTemporaryFile(delete=False, suffix=".layout-meta.xml", mode="w") as f:
        f.write(gen["layout_xml"])
        gen_layout_path = f.name
    with tempfile.NamedTemporaryFile(delete=False, suffix=".flexipage-meta.xml", mode="w") as f:
        f.write(gen["flexipage_xml"])
        gen_flexipage_path = f.name

    report_path = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w")
    json.dump(gen["report"], report_path)
    report_path.close()

    consistency = check_no_leaked_flags(report_path.name, gen_layout_path, gen_flexipage_path)
    if consistency["status"] == "PASS":
        st.success(f"Self-consistency check: PASS \u2014 {consistency['flagged_count']} flagged items, "
                   f"none leaked into the generated output.")
    else:
        st.error(f"Self-consistency check: FAIL \u2014 leaked items: {consistency['leaked_into_generated_output']}")

    st.subheader("Optional: compare against a known-correct reference")
    ref_layout_file = st.file_uploader("Reference Page Layout XML (optional)", type=["xml"], key="ref_layout")
    ref_flexipage_file = st.file_uploader("Reference Flexipage XML (optional)", type=["xml"], key="ref_flexipage")

    if ref_layout_file or ref_flexipage_file:
        if st.button("Run reference comparison"):
            if ref_layout_file:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".xml") as f:
                    f.write(ref_layout_file.getvalue())
                    ref_layout_path = f.name
                result = compare_layouts(ref_layout_path, gen_layout_path)
                st.write("**Page Layout comparison:**", result["status"])
                st.json(result)

            if ref_flexipage_file:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".xml") as f:
                    f.write(ref_flexipage_file.getvalue())
                    ref_flexipage_path = f.name
                result = compare_flexipages(ref_flexipage_path, gen_flexipage_path)
                st.write("**Flexipage comparison:**", result["status"])
                st.json(result)

    os.unlink(gen_layout_path)
    os.unlink(gen_flexipage_path)
    os.unlink(report_path.name)
