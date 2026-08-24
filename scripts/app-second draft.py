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
import subprocess
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
st.caption("Extract \u2192 Review & Approve \u2192 Generate \u2192 Validate \u2192 Deploy")

RESOURCE_LINKS = {
    "Field": [
        ("Dynamic Forms overview (Salesforce Help)",
         "https://help.salesforce.com/s/articleView?id=sf.dynamic_forms_overview.htm"),
        ("Field-Level Security basics",
         "https://help.salesforce.com/s/articleView?id=sf.admin_fls.htm"),
    ],
    "Related List": [
        ("Related Lists on Lightning pages",
         "https://help.salesforce.com/s/articleView?id=sf.lightning_page_related_lists.htm"),
    ],
    "Custom Button": [
        ("Custom Buttons/Links overview",
         "https://help.salesforce.com/s/articleView?id=sf.customize_buttonslinks.htm"),
    ],
    "Embedded Lightning Page": [
        ("Lightning Web Components dev guide",
         "https://developer.salesforce.com/docs/component-library/documentation/en/lwc"),
    ],
    "Setting": [
        ("Compact Layouts",
         "https://help.salesforce.com/s/articleView?id=sf.admin_compactlayout_overview.htm"),
    ],
}


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
with st.expander("Upload files & run extraction", expanded=(st.session_state.classified is None)):
    col1, col2 = st.columns(2)
    with col1:
        object_name = st.text_input("Salesforce Object", value="Account")
        layout_name = st.text_input("Layout Name", value="SP_Admin_Layout_HCO")
    with col2:
        veeva_file = st.file_uploader("Veeva layout XML", type=["xml"])
        registry_file = st.file_uploader("Mapping Registry JSON (existing)", type=["json"])

    run_clicked = st.button("Run Extraction + Classification", type="primary")

if run_clicked:
    if not veeva_file or not registry_file:
        st.error("Please upload both the Veeva layout XML and the Mapping Registry JSON.")
    else:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xml") as tmp_xml:
            tmp_xml.write(veeva_file.getvalue())
            xml_path = tmp_xml.name
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8") as tmp_reg:
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

    with st.expander("\U0001F4DA Helpful resources for reviewers", expanded=False):
        st.caption("Quick reference links by element type \u2014 useful when you're not sure "
                   "how something should behave in LSC/Dynamic Forms.")
        for etype, links in RESOURCE_LINKS.items():
            st.markdown(f"**{etype}**")
            for label, url in links:
                st.markdown(f"- [{label}]({url})")

    with st.expander("\U0001F916 Optional: get an AI second opinion on flagged rows", expanded=False):
        st.caption("This calls Claude directly from your machine to sanity-check anything "
                   "marked 'Decision Needed' or low confidence \u2014 purely advisory, it does NOT "
                   "change any row automatically. Requires your own Anthropic API key (not stored, "
                   "only used for this session).")
        api_key = st.text_input("Anthropic API key", type="password", key="anthropic_key")
        if st.button("Get AI second opinion on flagged rows") and api_key:
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=api_key)
                flagged = [e for e in classified if e["action"] in
                           ("flag_decision_needed", "flag_no_registry_entry", "flag_manual_review")]
                if not flagged:
                    st.info("No flagged rows to review \u2014 everything already matched confidently.")
                else:
                    summary_lines = [f"- {e['element_type']} '{e['api_name']}' in section "
                                      f"'{e['layout_section']}': currently flagged as {e['action']}. "
                                      f"Basis: {e.get('basis', 'none')}" for e in flagged[:20]]
                    prompt = (
                        "You are reviewing a Veeva-to-Salesforce-LSC page layout migration. "
                        "For each flagged item below, give a ONE-LINE suggestion of what a human "
                        "reviewer should check or consider, in plain language. Do not invent exact "
                        "API names you cannot know. Be concise.\n\n" + "\n".join(summary_lines)
                    )
                    with st.spinner("Asking Claude..."):
                        resp = client.messages.create(
                            model="claude-sonnet-4-6",
                            max_tokens=1000,
                            messages=[{"role": "user", "content": prompt}],
                        )
                    ai_text = "".join(b.text for b in resp.content if b.type == "text")
                    st.markdown("**AI second opinion (advisory only \u2014 review before trusting):**")
                    st.markdown(ai_text)
            except Exception as e:
                st.error(f"Could not reach Claude: {e}")
        elif not api_key:
            st.caption("Enter an API key above to enable this.")

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

    st.markdown("**Bulk actions** \u2014 useful when reviewing many rows at once (e.g. a new layout like HCP):")
    bulk_col1, bulk_col2, bulk_col3 = st.columns([2, 2, 1])
    with bulk_col1:
        bulk_confidence = st.selectbox("Approve all rows with confidence \u2265",
                                        ["High only", "High + Medium", "All (not recommended)"])
    with bulk_col2:
        st.caption("Only affects rows currently proposed 'auto_generate' or 'Direct/Map' \u2014 "
                   "never bulk-approves anything already flagged Rebuild/Retire/Decision Needed.")
    with bulk_col3:
        bulk_apply = st.button("Apply bulk approval")

    if bulk_apply:
        threshold = {"High only": {"High"}, "High + Medium": {"High", "Medium"},
                     "All (not recommended)": {"High", "Medium", "Low", "N/A"}}[bulk_confidence]
        applied_count = 0
        for i, e in enumerate(classified):
            conf = e.get("target", {}).get("confidence", "N/A")
            # Only promotes rows sitting at flag_manual_review (a registry match
            # that already HAS a proposed target, just needed confirmation because
            # of its confidence level) — never touches flag_no_registry_entry
            # (no target exists at all), flag_rebuild/retire/decision_needed
            # (genuine judgment calls), or rows already auto_generate.
            if e.get("action") == "flag_manual_review" and conf in threshold:
                df.at[i, "Action"] = "auto_generate"
                applied_count += 1
        if applied_count:
            st.success(f"Bulk-confirmed {applied_count} row(s) at '{bulk_confidence}' confidence "
                       f"from 'needs review' to 'auto_generate'. Rebuild/Retire/Decision-Needed rows "
                       f"and anything with no registry target were left untouched \u2014 those still "
                       f"need individual review below.")
        else:
            st.info("No rows matched \u2014 either nothing is at 'flag_manual_review', or none meet "
                   "the selected confidence threshold.")

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
    with st.expander("Run validation checks", expanded=True):
        st.caption("Self-consistency check always runs. Reference comparison is optional "
                   "(only if you have a known-correct hand-built file to check against, e.g. HCO).")

        gen_layout_path = gen_flexipage_path = report_json_path = None
        try:
            # Write generated files to temp paths so the existing validator
            # functions (which parse from disk) can run unmodified.
            with tempfile.NamedTemporaryFile(delete=False, suffix=".layout-meta.xml", mode="w", encoding="utf-8") as f:
                f.write(gen["layout_xml"])
                gen_layout_path = f.name
            with tempfile.NamedTemporaryFile(delete=False, suffix=".flexipage-meta.xml", mode="w", encoding="utf-8") as f:
                f.write(gen["flexipage_xml"])
                gen_flexipage_path = f.name

            with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8") as f:
                json.dump(gen["report"], f)
                report_json_path = f.name

            consistency = check_no_leaked_flags(report_json_path, gen_layout_path, gen_flexipage_path)
            if consistency["status"] == "PASS":
                st.success(f"Self-consistency check: PASS \u2014 {consistency['flagged_count']} flagged "
                           f"items, none leaked into the generated output.")
            else:
                st.error(f"Self-consistency check: FAIL \u2014 leaked items: "
                        f"{consistency['leaked_into_generated_output']}")
        except Exception as e:
            st.error("Self-consistency check hit an error. Full details below \u2014 "
                     "please share this with the accelerator team if you see it:")
            st.exception(e)

        st.subheader("Optional: compare against a known-correct reference")
        ref_layout_file = st.file_uploader("Reference Page Layout XML (optional)", type=["xml"], key="ref_layout")
        ref_flexipage_file = st.file_uploader("Reference Flexipage XML (optional)", type=["xml"], key="ref_flexipage")

        if (ref_layout_file or ref_flexipage_file) and st.button("Run reference comparison"):
            try:
                if ref_layout_file and gen_layout_path:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".xml") as f:
                        f.write(ref_layout_file.getvalue())
                        ref_layout_path = f.name
                    result = compare_layouts(ref_layout_path, gen_layout_path)
                    st.write("**Page Layout comparison:**", result["status"])
                    st.json(result)

                if ref_flexipage_file and gen_flexipage_path:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".xml") as f:
                        f.write(ref_flexipage_file.getvalue())
                        ref_flexipage_path = f.name
                    result = compare_flexipages(ref_flexipage_path, gen_flexipage_path)
                    st.write("**Flexipage comparison:**", result["status"])
                    st.json(result)
            except Exception as e:
                st.error("Reference comparison hit an error. Full details below:")
                st.exception(e)

        for p in (gen_layout_path, gen_flexipage_path, report_json_path):
            if p and os.path.exists(p):
                os.unlink(p)

    # -----------------------------------------------------------------
    # Step 5: Deploy
    # -----------------------------------------------------------------
    st.header("5. Deploy")
    with st.expander("Stage & deploy to a Salesforce org", expanded=False):
        st.caption("This stages the generated files into your SFDX project and runs a "
                   "**dry-run only** \u2014 it never deploys for real without a second explicit step.")

        migration_confirmed = st.checkbox(
            "I confirm the required fields/objects already exist in the target org "
            "(data migration or field creation has been done there)."
        )

        col1, col2 = st.columns(2)
        with col1:
            sfdx_root = st.text_input("SFDX project root path", value=".")
        with col2:
            target_org = st.text_input("Target org alias", value="lsc-new-org")

        deploy_clicked = st.button("Stage files + Dry-run Deploy", disabled=not migration_confirmed)
        if not migration_confirmed:
            st.caption("\u26a0\ufe0f Checkbox above must be ticked \u2014 deploying against an org "
                       "missing required fields will fail regardless of how correct this tool is.")

        if deploy_clicked:
            sfdx_project_json = os.path.join(sfdx_root, "sfdx-project.json")
            if not os.path.isfile(sfdx_project_json):
                st.error(f"'{sfdx_project_json}' not found \u2014 SFDX project root path looks wrong.")
            else:
                layouts_dir = os.path.join(sfdx_root, "force-app", "main", "default", "layouts")
                flexipages_dir = os.path.join(sfdx_root, "force-app", "main", "default", "flexipages")
                os.makedirs(layouts_dir, exist_ok=True)
                os.makedirs(flexipages_dir, exist_ok=True)

                final_layout_name = f"{object_name}-{layout_name}.layout-meta.xml"
                final_flexipage_name = f"{object_name}_{layout_name}_Record_Page.flexipage-meta.xml"
                layout_dest = os.path.join(layouts_dir, final_layout_name)
                flexipage_dest = os.path.join(flexipages_dir, final_flexipage_name)

                with open(layout_dest, "w", encoding="utf-8") as f:
                    f.write(gen["layout_xml"])
                with open(flexipage_dest, "w", encoding="utf-8") as f:
                    f.write(gen["flexipage_xml"])
                st.success(f"Staged:\n- {layout_dest}\n- {flexipage_dest}")

                cmd = [
                    "sf", "project", "deploy", "start",
                    "--source-dir", f"force-app/main/default/layouts/{final_layout_name}",
                    f"force-app/main/default/flexipages/{final_flexipage_name}",
                    "--target-org", target_org,
                    "--dry-run",
                ]
                st.code(" ".join(cmd), language="bash")
                try:
                    with st.spinner("Running dry-run deploy..."):
                        result = subprocess.run(cmd, cwd=sfdx_root, capture_output=True,
                                                 text=True, timeout=120)
                    st.code(result.stdout or "(no stdout)")
                    if result.returncode != 0:
                        st.error("Dry-run reported errors (see above) \u2014 not a bug in this tool; "
                                "usually means a referenced field/object/record type doesn't exist "
                                "in the target org yet.")
                        if result.stderr:
                            st.code(result.stderr)
                    else:
                        st.success("Dry-run succeeded with no errors. Safe to deploy for real via "
                                  "the same command without --dry-run.")
                except FileNotFoundError:
                    st.error("Could not find the 'sf' CLI on this machine. Make sure Salesforce CLI "
                            "is installed and this app is run from a terminal where 'sf --version' works.")
                except Exception as e:
                    st.error("Dry-run deploy hit an unexpected error:")
                    st.exception(e)
