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
import shutil
import subprocess
import tempfile

import pandas as pd
import streamlit as st

def resolve_sf_executable():
    """On Windows, Salesforce CLI installs as 'sf.cmd' (an npm wrapper
    script), not a plain 'sf.exe'. subprocess.run(['sf', ...]) looks for an
    exact match and fails with FileNotFoundError even though 'sf' works
    fine typed directly in a terminal (the shell resolves .cmd extensions
    automatically; Python's subprocess does not, unless shell=True).
    shutil.which() correctly searches PATHEXT (.COM/.EXE/.BAT/.CMD) and
    returns the real resolvable path -- use that instead of a bare 'sf'."""
    resolved = shutil.which("sf")
    return resolved if resolved else "sf"

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


def group_all_by_section_before(elements):
    """'Before' view: EVERY field found on the original Veeva layout,
    regardless of what the Rules Engine decided to do with it \u2014 this is
    intentionally unfiltered, showing the layout exactly as it looked
    originally, warts and all. Uses the same section/column grouping shape
    as generator.group_fields_by_section for a fair visual comparison, but
    does NOT filter by action."""
    sections = {}
    order = []
    for e in elements:
        if e["element_type"] != "Field":
            continue
        sec = e["layout_section"]
        if sec == "Mini/Compact Layout":
            continue
        if sec not in sections:
            sections[sec] = {}
            order.append(sec)
        col = e.get("column_index", 1)
        sections[sec].setdefault(col, []).append(e)
    return order, sections


def render_layout_mockup(section_order, sections_by_col, style, badge_fn=None):
    """Renders a simple HTML mockup of a page layout \u2014 a visual aid for
    demos, NOT a literal screenshot of any real Salesforce org. style is
    'classic' (muted, Veeva/Salesforce-Classic-like) or 'lightning' (cleaner
    card style, LSC-like)."""
    if style == "classic":
        section_bg, section_border, header_bg = "#f4f4f2", "#c9c9c7", "#5f7a99"
    else:
        section_bg, section_border, header_bg = "#ffffff", "#d8dde6", "#0b5cab"

    html = ['<div style="font-family: -apple-system, Segoe UI, Arial, sans-serif;">']
    for section in section_order:
        cols = sections_by_col[section]
        html.append(
            f'<div style="border:1px solid {section_border}; border-radius:6px; '
            f'margin-bottom:12px; overflow:hidden;">'
            f'<div style="background:{header_bg}; color:white; padding:6px 12px; '
            f'font-weight:600; font-size:13px;">{section}</div>'
            f'<div style="display:flex; background:{section_bg}; padding:8px;">'
        )
        for col_idx in sorted(cols.keys()):
            html.append('<div style="flex:1; padding:4px 8px;">')
            for e in cols[col_idx]:
                name = e.get("target", {}).get("api_name") or e["api_name"]
                badge = badge_fn(e) if badge_fn else ""
                html.append(
                    f'<div style="padding:4px 0; font-size:12px; border-bottom:1px solid #eee; '
                    f'display:flex; justify-content:space-between;">'
                    f'<span>{name}</span>{badge}</div>'
                )
            html.append("</div>")
        html.append("</div></div>")
    html.append("</div>")
    return "".join(html)


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
        input_mode = st.radio("Veeva layout source", ["Upload file", "Fetch directly from Veeva org"], horizontal=True)
        fetch_clicked = False
        if input_mode == "Upload file":
            veeva_file = st.file_uploader("Veeva layout XML", type=["xml"])
        else:
            veeva_file = None
            veeva_org_alias = st.text_input(
                "Veeva org alias (already authenticated via 'sf org login web')",
                value="veevaSource")
            veeva_sfdx_root = st.text_input("SFDX project root path", value="..", key="veeva_fetch_root")
            fetch_clicked = st.button("Fetch layout from Veeva org")
        registry_file = st.file_uploader("Mapping Registry JSON (existing)", type=["json"])
        describe_file = st.file_uploader(
            "Optional: target org's sobject describe JSON (from 'sf sobject describe --json') "
            "\u2014 verifies flagged fields against what actually exists in the org",
            type=["json"], key="describe_upload")

    if input_mode == "Fetch directly from Veeva org" and fetch_clicked:
        from veeva_fetch import fetch_veeva_layout
        with st.spinner(f"Looking for {object_name}/{layout_name} in {veeva_org_alias}..."):
            fetch_result = fetch_veeva_layout(object_name, layout_name, veeva_org_alias, veeva_sfdx_root)
        st.session_state.veeva_fetch_result = fetch_result

    if st.session_state.get("veeva_fetch_result"):
        fr = st.session_state.veeva_fetch_result
        if fr["status"] in ("FOUND_DIRECT", "FOUND_FUZZY"):
            st.success(f"\u2713 {fr['message']}")
            if os.path.isfile(fr["file_path"]):
                with open(fr["file_path"], "rb") as f:
                    veeva_file_bytes = f.read()
                # Wrap in a lightweight object matching Streamlit's UploadedFile
                # interface (.getvalue()) so downstream code works unchanged
                # regardless of whether the file came from upload or fetch.
                class _FetchedFile:
                    def __init__(self, data):
                        self._data = data
                    def getvalue(self):
                        return self._data
                veeva_file = _FetchedFile(veeva_file_bytes)
            else:
                st.warning(f"Retrieval reported success but the file wasn't found at {fr['file_path']} \u2014 check the SFDX project root path.")
        elif fr["status"] == "AMBIGUOUS":
            st.warning(fr["message"])
            for c in fr["candidates"]:
                assignments = c.get("profile_assignments") or []
                if assignments:
                    assignment_str = ", ".join(
                        f"{a['profile']}" + (f" ({a['record_type']})" if a.get("record_type") else "")
                        for a in assignments
                    )
                    st.write(f"  \u2022 **{c['full_name']}** (similarity {c['similarity']}) \u2014 assigned to: {assignment_str}")
                else:
                    st.write(f"  \u2022 **{c['full_name']}** (similarity {c['similarity']}) \u2014 no profile assignment info available")
            st.caption("Adjust the Object/Layout Name fields above to match one of these exactly, then fetch again.")
        else:
            st.error(fr["message"])

    run_clicked = st.button("Run Extraction + Classification", type="primary")

if run_clicked:
    if not veeva_file or not registry_file:
        missing = []
        if not veeva_file:
            missing.append("a Veeva layout (upload a file, or fetch one from the Veeva org above)")
        if not registry_file:
            missing.append("the Mapping Registry JSON")
        st.error(f"Please provide: {' and '.join(missing)}.")
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

        org_verification_summary = None
        if describe_file:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8") as tmp_desc:
                tmp_desc.write(describe_file.getvalue().decode("utf-8"))
                describe_path = tmp_desc.name
            try:
                from rules_engine import apply_org_verification
                org_verification_summary = apply_org_verification(classified, describe_path)
            except Exception as e:
                st.error("Org verification failed to run:")
                st.exception(e)
            finally:
                os.unlink(describe_path)

        st.session_state.classified = classified
        st.session_state.registry_conflicts = registry_conflicts
        st.session_state.duplicate_conflicts = inconsistencies
        st.session_state.org_verification_summary = org_verification_summary
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

    if st.session_state.get("org_verification_summary"):
        s = st.session_state.org_verification_summary
        st.success(f"\U0001F50D Org verification ran: {s['org_verified_count']} field(s) confirmed to "
                   f"exist in the target org (upgraded to auto_generate), "
                   f"{s['candidates_found_count']} field(s) got real candidate suggestions "
                   f"(still need your review \u2014 see 'Candidates' column below).")

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
        candidates = e.get("suggested_candidates", [])
        def format_candidate(c):
            # Field candidates use 'api_name'; Related List candidates use
            # 'child_object' instead \u2014 two different shapes from
            # org_verifier.py's find_candidate_fields vs
            # find_candidate_related_lists. Handle both rather than
            # assuming one.
            name = c.get("api_name") or c.get("child_object") or "?"
            return f"{name} ({c['similarity']})"

        candidates_str = ", ".join(format_candidate(c) for c in candidates) if candidates else "-"
        rows.append({
            "idx": i,
            "Section": e["layout_section"],
            "Type": e["element_type"],
            "API Name": e["api_name"],
            "Classification": e.get("classification", {}).get("canonical", "-"),
            "Target": e.get("target", {}).get("api_name") or "-",
            "Confidence": e.get("target", {}).get("confidence", "-"),
            "Org Verified": "\u2713" if e.get("org_verified") else ("\u2717" if "org_verified" in e else "-"),
            "Candidates (from org)": candidates_str,
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
        width="stretch",
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

    tab1, tab2, tab3, tab4 = st.tabs(["Before / After Preview", "Page Layout XML", "Flexipage XML", "Build Report (not built)"])
    with tab1:
        st.caption("Upload a real screenshot for any panel to show the actual UI. If you skip a panel's "
                   "screenshot, it falls back to a rendered structural mockup instead \u2014 either way, "
                   "labeled clearly which one you're looking at.")

        before_order, before_sections = group_all_by_section_before(st.session_state.classified)
        after_order, after_sections = group_fields_by_section(gen["reviewed_elements"])

        def after_badge(e):
            return '<span style="color:#2e7d32; font-size:11px;">\u2713 built</span>'

        upload_col1, upload_col2, upload_col3 = st.columns(3)
        with upload_col1:
            veeva_screenshot = st.file_uploader("Veeva screenshot (optional)", type=["png", "jpg", "jpeg"], key="veeva_shot")
        with upload_col2:
            current_screenshot = st.file_uploader("Current org screenshot (optional)", type=["png", "jpg", "jpeg"], key="current_shot")
            current_org_file = st.file_uploader("...or upload the current layout XML instead", type=["xml"], key="current_org_layout")
        with upload_col3:
            after_screenshot = st.file_uploader("Deployed-org screenshot (optional, once deployed)", type=["png", "jpg", "jpeg"], key="after_shot")

        panel_col1, panel_col2, panel_col3 = st.columns(3)

        with panel_col1:
            st.markdown("**VEEVA \u2014 Original**")
            if veeva_screenshot:
                st.image(veeva_screenshot, width="stretch")
                st.caption("Real screenshot")
            else:
                st.markdown(f"*{sum(len(v) for c in before_sections.values() for v in c.values())} fields, unfiltered*")
                st.markdown(render_layout_mockup(before_order, before_sections, "classic"), unsafe_allow_html=True)
                st.caption("Rendered mockup (no screenshot uploaded)")

        with panel_col2:
            st.markdown("**CURRENT ORG \u2014 Before this tool**")
            if current_screenshot:
                st.image(current_screenshot, width="stretch")
                st.caption("Real screenshot")
            elif current_org_file:
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".xml") as f:
                        f.write(current_org_file.getvalue())
                        current_org_path = f.name
                    current_fields = parse_layout_fields(current_org_path)
                    os.unlink(current_org_path)
                    current_sections, current_order = {}, []
                    for (section, col, field_name), behavior in current_fields.items():
                        if section not in current_sections:
                            current_sections[section] = {}
                            current_order.append(section)
                        current_sections[section].setdefault(col, []).append({"api_name": field_name, "target": {}})
                    st.markdown(f"*{len(current_fields)} fields, as deployed today*")
                    st.markdown(render_layout_mockup(current_order, current_sections, "classic"), unsafe_allow_html=True)
                    st.caption("Rendered mockup from uploaded XML")
                except Exception as e:
                    st.error("Could not parse that XML file:")
                    st.exception(e)
            else:
                st.info("Upload a screenshot or the current layout XML to show this panel.")

        with panel_col3:
            st.markdown("**AFTER \u2014 Generated by this tool**")
            if after_screenshot:
                st.image(after_screenshot, width="stretch")
                st.caption("Real screenshot \u2014 confirmed deployed")
            else:
                st.markdown(f"*{sum(len(v) for c in after_sections.values() for v in c.values())} fields, auto-built only*")
                st.markdown(render_layout_mockup(after_order, after_sections, "lightning", badge_fn=after_badge), unsafe_allow_html=True)
                st.caption("Rendered mockup (not yet deployed \u2014 upload a screenshot once it is)")

        st.info(f"Note: {sum(len(v) for c in before_sections.values() for v in c.values()) - sum(len(v) for c in after_sections.values() for v in c.values())} "
               f"field(s) present in the original Veeva layout are NOT in the After view \u2014 these are "
               f"the items flagged for rebuild/retire/decision, listed in the Build Report tab, not silently dropped.")

    with tab2:
        st.code(gen["layout_xml"], language="xml")
        st.download_button("Download Layout XML", gen["layout_xml"],
                            file_name=f"{object_name}-{layout_name}_Generated.layout-meta.xml")
    with tab3:
        st.code(gen["flexipage_xml"], language="xml")
        st.download_button("Download Flexipage XML", gen["flexipage_xml"],
                            file_name=f"{object_name}_{layout_name}_Generated.flexipage-meta.xml")
    with tab4:
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
    with st.expander("Stage, pre-flight check, deploy, and verify", expanded=False):
        st.caption("Full flow: stage files \u2192 check record types exist \u2192 dry-run \u2192 "
                   "real deploy (separate confirmation) \u2192 fetch back and verify it matches.")

        migration_confirmed = st.checkbox(
            "I confirm the required fields/objects already exist in the target org "
            "(data migration or field creation has been done there)."
        )

        col1, col2 = st.columns(2)
        with col1:
            sfdx_root = st.text_input("SFDX project root path", value=".")
        with col2:
            target_org = st.text_input("Target org alias", value="lsc-new-org")

        # --- Pre-flight: record type check -------------------------------
        st.subheader("5a. Record type pre-flight check")
        st.caption("Catches the exact failure mode from manual migration experience: a layout "
                   "that depends on a record type nobody created in the target org yet.")
        rt_col1, rt_col2 = st.columns([2, 2])
        with rt_col1:
            record_types_input = st.text_input(
                "Record type name(s) this layout is assigned to (comma-separated)",
                placeholder="e.g. HCO  or  HCP,Master")
        with rt_col2:
            rt_describe_file = st.file_uploader(
                "Org describe JSON (reuse the one from Step 1, or upload again)",
                type=["json"], key="deploy_describe_upload")

        if st.button("Check record types") and record_types_input and rt_describe_file:
            try:
                from org_verifier import load_describe, check_record_types
                with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8") as f:
                    f.write(rt_describe_file.getvalue().decode("utf-8"))
                    rt_describe_path = f.name
                describe_data = load_describe(rt_describe_path)
                os.unlink(rt_describe_path)

                names = [n.strip() for n in record_types_input.split(",") if n.strip()]
                rt_results = check_record_types(describe_data, names)
                all_exist = all(r["exists"] for r in rt_results)
                for r in rt_results:
                    if r["exists"]:
                        st.success(f"\u2713 Record type '{r['record_type']}' exists (active={r['active']})")
                    else:
                        st.error(f"\u2717 Record type '{r['record_type']}' does NOT exist in {target_org} \u2014 "
                                f"deploying this layout's record-type assignment will fail until it's created.")
                st.session_state.record_types_ready = all_exist
            except Exception as e:
                st.error("Record type check failed:")
                st.exception(e)
        elif not (record_types_input and rt_describe_file):
            st.caption("Enter record type name(s) and upload a describe file to run this check.")

        st.divider()

        # --- Stage + Dry-run -----------------------------------------------
        st.subheader("5b. Stage & Dry-run")
        deploy_clicked = st.button("Stage files + Dry-run Deploy", disabled=not migration_confirmed)
        if not migration_confirmed:
            st.caption("\u26a0\ufe0f Checkbox above must be ticked \u2014 deploying against an org "
                       "missing required fields will fail regardless of how correct this tool is.")

        final_layout_name = f"{object_name}-{layout_name}.layout-meta.xml"
        final_flexipage_name = f"{object_name}_{layout_name}_Record_Page.flexipage-meta.xml"

        if deploy_clicked:
            sfdx_project_json = os.path.join(sfdx_root, "sfdx-project.json")
            if not os.path.isfile(sfdx_project_json):
                st.error(f"'{sfdx_project_json}' not found \u2014 SFDX project root path looks wrong.")
            else:
                layouts_dir = os.path.join(sfdx_root, "force-app", "main", "default", "layouts")
                flexipages_dir = os.path.join(sfdx_root, "force-app", "main", "default", "flexipages")
                os.makedirs(layouts_dir, exist_ok=True)
                os.makedirs(flexipages_dir, exist_ok=True)

                layout_dest = os.path.join(layouts_dir, final_layout_name)
                flexipage_dest = os.path.join(flexipages_dir, final_flexipage_name)

                with open(layout_dest, "w", encoding="utf-8") as f:
                    f.write(gen["layout_xml"])
                with open(flexipage_dest, "w", encoding="utf-8") as f:
                    f.write(gen["flexipage_xml"])
                st.success(f"Staged:\n- {layout_dest}\n- {flexipage_dest}")

                cmd = [
                    resolve_sf_executable(), "project", "deploy", "start",
                    "--source-dir", f"force-app/main/default/layouts/{final_layout_name}",
                    f"force-app/main/default/flexipages/{final_flexipage_name}",
                    "--target-org", target_org, "--dry-run",
                ]
                st.code(" ".join(cmd), language="bash")
                try:
                    with st.spinner("Running dry-run deploy..."):
                        result = subprocess.run(cmd, cwd=sfdx_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                    st.code(result.stdout or "(no stdout)")
                    if result.returncode != 0:
                        st.error("Dry-run reported errors \u2014 usually a missing field/object/record type "
                                "in the target org, not a bug in this tool.")
                        if result.stderr:
                            st.code(result.stderr)
                        st.session_state.dry_run_passed = False
                    else:
                        st.success("Dry-run succeeded with no errors.")
                        st.session_state.dry_run_passed = True
                except FileNotFoundError:
                    st.error("Could not find the 'sf' CLI. Make sure Salesforce CLI is installed.")
                    st.session_state.dry_run_passed = False
                except Exception as e:
                    st.error("Dry-run deploy hit an unexpected error:")
                    st.exception(e)
                    st.session_state.dry_run_passed = False

        st.divider()

        # --- Real deploy: gated behind a passed dry-run + explicit confirmation
        st.subheader("5c. Real deploy")
        dry_run_ok = st.session_state.get("dry_run_passed", False)
        if not dry_run_ok:
            st.caption("Run a successful dry-run above first \u2014 this button stays disabled until then.")
        real_deploy_confirmed = st.checkbox(
            "I've reviewed the dry-run output above and want to deploy this for real, now.",
            disabled=not dry_run_ok)
        real_deploy_clicked = st.button("Deploy for real", disabled=not (dry_run_ok and real_deploy_confirmed))

        if real_deploy_clicked:
            cmd = [
                resolve_sf_executable(), "project", "deploy", "start",
                "--source-dir", f"force-app/main/default/layouts/{final_layout_name}",
                f"force-app/main/default/flexipages/{final_flexipage_name}",
                "--target-org", target_org,
            ]
            st.code(" ".join(cmd), language="bash")
            try:
                with st.spinner("Deploying for real..."):
                    result = subprocess.run(cmd, cwd=sfdx_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
                st.code(result.stdout or "(no stdout)")
                if result.returncode == 0:
                    st.success("\u2705 Deployed successfully. Proceed to 5d to verify it landed correctly.")
                    st.session_state.deploy_succeeded = True
                else:
                    st.error("Real deploy failed \u2014 see output above.")
                    if result.stderr:
                        st.code(result.stderr)
                    st.session_state.deploy_succeeded = False
            except Exception as e:
                st.error("Real deploy hit an unexpected error:")
                st.exception(e)

        st.divider()

        # --- Post-deploy verification: fetch it back and diff -------------
        st.subheader("5d. Post-deploy verification")
        st.caption("Closes the loop: pulls the layout back FROM the org after deploying, and confirms "
                   "it actually matches what we generated \u2014 not just trusting the deploy succeeded.")
        if st.button("Fetch deployed layout & verify", disabled=not st.session_state.get("deploy_succeeded", False)):
            retrieve_cmd = [
                resolve_sf_executable(), "project", "retrieve", "start",
                "--metadata", f"Layout:{object_name}-{layout_name}",
                "--target-org", target_org,
            ]
            st.code(" ".join(retrieve_cmd), language="bash")
            try:
                with st.spinner("Retrieving deployed layout from the org..."):
                    result = subprocess.run(retrieve_cmd, cwd=sfdx_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
                st.code(result.stdout or "(no stdout)")
                if result.returncode != 0:
                    st.error("Could not retrieve the layout back from the org:")
                    if result.stderr:
                        st.code(result.stderr)
                else:
                    retrieved_path = os.path.join(sfdx_root, "force-app", "main", "default",
                                                   "layouts", final_layout_name)
                    if os.path.isfile(retrieved_path):
                        retrieved_fields = parse_layout_fields(retrieved_path)
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".layout-meta.xml", mode="w", encoding="utf-8") as f:
                            f.write(gen["layout_xml"])
                            generated_path = f.name
                        diff_result = compare_layouts(generated_path, retrieved_path)
                        os.unlink(generated_path)
                        if diff_result["status"] == "PASS":
                            st.success(f"\u2705 VERIFIED: the layout as deployed in {target_org} matches "
                                      f"exactly what this tool generated ({diff_result['total_reference_fields']} fields).")
                        else:
                            st.error("\u26a0\ufe0f Deployed layout does NOT match the generated file:")
                            st.json(diff_result)
                    else:
                        st.warning(f"Retrieve succeeded but couldn't find the file at {retrieved_path} to compare.")
            except Exception as e:
                st.error("Post-deploy verification hit an unexpected error:")
                st.exception(e)
