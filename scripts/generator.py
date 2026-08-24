#!/usr/bin/env python3
"""
Veeva -> LSC Page Layout Accelerator — Layer 4: Generation

Reads Layer 3's classified output and generates the two real metadata files
an LSC layout needs: a Flexipage XML (Dynamic Forms field placement) and a
Page Layout XML (related lists, buttons, quick actions, legacy settings).

SCOPE OF THIS PASS (mirrors the human-built HCO reference's own "Build Pass 1"
comment, which took the same approach manually):
  - Generates Dynamic Forms field sections for every Field element with
    action == "auto_generate" — i.e. registry-confirmed, high-confidence only.
  - Generates the Page Layout's field sections, miniLayout, and any
    registry-confirmed related lists / quick actions with action ==
    "auto_generate".
  - Everything else (flag_rebuild, flag_retire, flag_decision_needed,
    flag_manual_review, flag_no_registry_entry) is deliberately NOT built —
    it's written out to a companion "build_report" so nothing is silently
    dropped or silently guessed.
  - Standard boilerplate regions (highlights panel header, chatter feed,
    sidebar tabs, related list container, standard action buttons,
    excludeButtons) are templated constants copied from the proven HCO
    structure — NOT derived from the registry. These are object/org-level
    defaults, not layout-specific judgment calls, but they SHOULD be
    reviewed if this is ever pointed at a different object.

Usage:
    python generator.py --object Account --layout SP_Admin_Layout_HCO \
        --classified classified_SP_Admin_Layout_HCO.json \
        --outdir generated/
"""

import argparse
import json
import os
import re

BEHAVIOR_TO_UI = {
    "Required": "required",
    "Edit": "none",
    "Readonly": "readonly",
}

# Sections with only 1 populated column render OneColumn; System Information
# in the HCO reference uses TwoColumnsTopToBottom rather than LeftToRight —
# that distinction isn't derivable from the Veeva source structure alone, so
# it's kept here as a known, documented exception list rather than guessed.
SECTION_STYLE_OVERRIDES = {
    "System Information": "TwoColumnsTopToBottom",
}


def safe_identifier(text):
    return re.sub(r"[^A-Za-z0-9]", "", text)


# "Mini/Compact Layout" is not a real Page Layout section — the extractor
# tags miniLayout fields as element_type "Field" for consistency, but they
# only ever populate the separate <miniLayout> block, never a layoutSection
# or a Flexipage field section. Excluded here explicitly rather than
# silently mis-generating a fake section for it (a real bug caught during
# validation against the HCO reference — see build notes).
NON_SECTION_LABELS = {"Mini/Compact Layout"}


def group_fields_by_section(elements):
    sections = {}
    order = []
    for e in elements:
        if e["element_type"] != "Field" or e["action"] != "auto_generate":
            continue
        sec = e["layout_section"]
        if sec in NON_SECTION_LABELS:
            continue
        if sec not in sections:
            sections[sec] = {}
            order.append(sec)
        col = e.get("column_index", 1)
        sections[sec].setdefault(col, []).append(e)
    return order, sections


def build_report(elements):
    """Everything NOT auto-generated, grouped by action, so nothing silently
    disappears between classification and generation."""
    report = {}
    for e in elements:
        if e["action"] == "auto_generate":
            continue
        if e["element_type"].startswith("_"):
            continue
        report.setdefault(e["action"], []).append({
            "layout_section": e["layout_section"],
            "element_type": e["element_type"],
            "api_name": e["api_name"],
            "basis": e.get("basis"),
        })
    return report


# ---------------------------------------------------------------------------
# Flexipage generation
# ---------------------------------------------------------------------------

def gen_flexipage(section_order, sections_by_col, object_name, layout_name):
    facet_blocks = []
    fieldsection_wiring = []

    for section in section_order:
        cols = sections_by_col[section]
        section_id = safe_identifier(section)
        col_facet_names = []

        for col_idx in sorted(cols.keys()):
            facet_name = f"Facet_{section_id}_Col{col_idx}"
            col_facet_names.append(facet_name)
            field_instances = []
            for e in cols[col_idx]:
                ui_behavior = BEHAVIOR_TO_UI.get(e.get("behavior", "Edit"), "none")
                field_id = f"field{section_id}Col{col_idx}{safe_identifier(e['target']['api_name'])}"
                field_instances.append(f"""    <itemInstances>
        <fieldInstance>
            <fieldInstanceProperties>
                <name>uiBehavior</name>
                <value>{ui_behavior}</value>
            </fieldInstanceProperties>
            <fieldItem>Record.{e['target']['api_name']}</fieldItem>
            <identifier>{field_id}</identifier>
        </fieldInstance>
    </itemInstances>""")
            facet_blocks.append(f"""<flexiPageRegions>
{chr(10).join(field_instances)}
    <name>{facet_name}</name>
    <type>Facet</type>
</flexiPageRegions>""")

        cols_facet_name = f"Facet_{section_id}_Cols"
        column_components = []
        for i, fname in enumerate(col_facet_names, start=1):
            column_components.append(f"""        <itemInstances>
            <componentInstance>
                <componentInstanceProperties>
                    <name>body</name>
                    <value>{fname}</value>
                </componentInstanceProperties>
                <componentName>flexipage:column</componentName>
                <identifier>column_{cols_facet_name}_{i}</identifier>
            </componentInstance>
        </itemInstances>""")
        facet_blocks.append(f"""    <flexiPageRegions>
{chr(10).join(column_components)}
        <name>{cols_facet_name}</name>
        <type>Facet</type>
    </flexiPageRegions>""")

        fieldsection_wiring.append(f"""        <itemInstances>
            <componentInstance>
                <componentInstanceProperties>
                    <name>columns</name>
                    <value>{cols_facet_name}</value>
                </componentInstanceProperties>
                <componentInstanceProperties>
                    <name>horizontalAlignment</name>
                    <value>false</value>
                </componentInstanceProperties>
                <componentInstanceProperties>
                    <name>label</name>
                    <value>{section}</value>
                </componentInstanceProperties>
                <componentName>flexipage:fieldSection</componentName>
                <identifier>fieldSection_{section_id}</identifier>
            </componentInstance>
        </itemInstances>""")

    header_region = """    <flexiPageRegions>
        <itemInstances>
            <componentInstance>
                <componentInstanceProperties>
                    <name>collapsed</name>
                    <value>false</value>
                </componentInstanceProperties>
                <componentInstanceProperties>
                    <name>enableActionsConfiguration</name>
                    <value>false</value>
                </componentInstanceProperties>
                <componentInstanceProperties>
                    <name>enableActionsInNative</name>
                    <value>false</value>
                </componentInstanceProperties>
                <componentInstanceProperties>
                    <name>hideChatterActions</name>
                    <value>false</value>
                </componentInstanceProperties>
                <componentInstanceProperties>
                    <name>hideSlackAction</name>
                    <value>false</value>
                </componentInstanceProperties>
                <componentInstanceProperties>
                    <name>numVisibleActions</name>
                    <value>3</value>
                </componentInstanceProperties>
                <componentName>force:highlightsPanel</componentName>
                <identifier>force_highlightsPanel</identifier>
            </componentInstance>
        </itemInstances>
        <name>header</name>
        <type>Region</type>
    </flexiPageRegions>"""

    main_region = f"""    <flexiPageRegions>
{chr(10).join(fieldsection_wiring)}
        <itemInstances>
            <componentInstance>
                <componentInstanceProperties>
                    <name>relatedListComponentOverride</name>
                    <value>NONE</value>
                </componentInstanceProperties>
                <componentInstanceProperties>
                    <name>rowsToDisplay</name>
                    <value>10</value>
                </componentInstanceProperties>
                <componentInstanceProperties>
                    <name>showActionBar</name>
                    <value>true</value>
                </componentInstanceProperties>
                <componentName>force:relatedListContainer</componentName>
                <identifier>force_relatedListContainer</identifier>
            </componentInstance>
        </itemInstances>
        <name>main</name>
        <type>Region</type>
    </flexiPageRegions>"""

    feed_sidebar = """    <flexiPageRegions>
        <itemInstances>
            <componentInstance>
                <componentName>forceChatter:recordFeedContainer</componentName>
                <identifier>forceChatter_recordFeedContainer</identifier>
            </componentInstance>
        </itemInstances>
        <name>feedTabContent</name>
        <type>Facet</type>
    </flexiPageRegions>
    <flexiPageRegions>
        <itemInstances>
            <componentInstance>
                <componentInstanceProperties>
                    <name>active</name>
                    <value>true</value>
                </componentInstanceProperties>
                <componentInstanceProperties>
                    <name>body</name>
                    <value>feedTabContent</value>
                </componentInstanceProperties>
                <componentInstanceProperties>
                    <name>title</name>
                    <value>Standard.Tab.collaborate</value>
                </componentInstanceProperties>
                <componentName>flexipage:tab</componentName>
                <identifier>collaborateTab</identifier>
            </componentInstance>
        </itemInstances>
        <name>sidebartabs</name>
        <type>Facet</type>
    </flexiPageRegions>
    <flexiPageRegions>
        <itemInstances>
            <componentInstance>
                <componentInstanceProperties>
                    <name>label</name>
                    <value>Tabs</value>
                </componentInstanceProperties>
                <componentInstanceProperties>
                    <name>tabs</name>
                    <value>sidebartabs</value>
                </componentInstanceProperties>
                <componentName>flexipage:tabset</componentName>
                <identifier>flexipage_tabset2</identifier>
            </componentInstance>
        </itemInstances>
        <name>sidebar</name>
        <type>Region</type>
    </flexiPageRegions>"""

    body = "\n".join(facet_blocks)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<FlexiPage xmlns="http://soap.sforce.com/2006/04/metadata">
    <!--
        AUTO-GENERATED by Page Layout Accelerator (Layer 4: Generation).
        Source layout: {object_name} / {layout_name}
        Contains Direct + Map fields with action=auto_generate ONLY.
        See the companion build_report JSON for everything deliberately
        NOT included here (needs rebuild / retire sign-off / a decision /
        manual confirmation) — nothing was silently dropped.
    -->
{header_region}
{body}
{main_region}
{feed_sidebar}
    <masterLabel>{object_name}_{layout_name}_Generated</masterLabel>
    <sobjectType>{object_name}</sobjectType>
    <template>
        <name>flexipage:recordHomeTemplateDesktop</name>
    </template>
    <type>RecordPage</type>
</FlexiPage>
"""
    return xml


# ---------------------------------------------------------------------------
# Page Layout generation
# ---------------------------------------------------------------------------

def gen_page_layout(section_order, sections_by_col, elements, object_name, layout_name):
    section_blocks = []
    for section in section_order:
        cols = sections_by_col[section]
        col_count = len(cols)
        style = SECTION_STYLE_OVERRIDES.get(section, "OneColumn" if col_count == 1 else "TwoColumnsLeftToRight")

        col_blocks = []
        for col_idx in sorted(cols.keys()):
            items = []
            for e in cols[col_idx]:
                items.append(f"""            <layoutItems>
                <behavior>{e.get('behavior', 'Edit')}</behavior>
                <field>{e['target']['api_name']}</field>
            </layoutItems>""")
            col_blocks.append(f"        <layoutColumns>\n" + "\n".join(items) + "\n        </layoutColumns>")

        section_blocks.append(f"""    <layoutSections>
        <customLabel>true</customLabel>
        <detailHeading>true</detailHeading>
        <editHeading>true</editHeading>
        <label>{section}</label>
{chr(10).join(col_blocks)}
        <style>{style}</style>
    </layoutSections>""")

    # miniLayout: fields marked auto_generate under "Mini/Compact Layout"
    mini_fields = [e["target"]["api_name"] for e in elements
                   if e["layout_section"] == "Mini/Compact Layout"
                   and e["element_type"] == "Field" and e["action"] == "auto_generate"]
    mini_block = ""
    if mini_fields:
        mini_lines = "\n".join(f"        <fields>{f}</fields>" for f in mini_fields)
        mini_block = f"    <miniLayout>\n{mini_lines}\n    </miniLayout>\n"

    # Related lists confirmed auto_generate (exact strings from the registry)
    rl_blocks = []
    for e in elements:
        if e["element_type"] == "Related List" and e["action"] == "auto_generate":
            target = e["target"]["api_name"]
            rl_blocks.append(f"""    <relatedLists>
        <relatedList>{target}</relatedList>
    </relatedLists>""")

    # Quick actions confirmed auto_generate
    qa_names = [e["target"]["api_name"] for e in elements
                if e["element_type"] == "Quick Action" and e["action"] == "auto_generate"]
    qa_block = ""
    if qa_names:
        qa_lines = "\n".join(f"            <quickActionName>{n}</quickActionName>" for n in qa_names)
        qa_block = f"    <quickActionList>\n        <quickActionListItems>\n{qa_lines}\n        </quickActionListItems>\n    </quickActionList>\n"

    # Standard action buttons + excludeButtons: templated constants, not
    # registry-derived — same rationale as the docstring above.
    standard_actions = """    <platformActionList>
        <actionListContext>Record</actionListContext>
        <platformActionListItems>
            <actionName>Edit</actionName>
            <actionType>StandardButton</actionType>
            <sortOrder>0</sortOrder>
        </platformActionListItems>
        <platformActionListItems>
            <actionName>Delete</actionName>
            <actionType>StandardButton</actionType>
            <sortOrder>1</sortOrder>
        </platformActionListItems>
        <platformActionListItems>
            <actionName>ChangeOwnerOne</actionName>
            <actionType>StandardButton</actionType>
            <sortOrder>2</sortOrder>
        </platformActionListItems>
        <platformActionListItems>
            <actionName>ChangeRecordType</actionName>
            <actionType>StandardButton</actionType>
            <sortOrder>3</sortOrder>
        </platformActionListItems>
        <platformActionListItems>
            <actionName>PrintableView</actionName>
            <actionType>StandardButton</actionType>
            <sortOrder>4</sortOrder>
        </platformActionListItems>
    </platformActionList>
"""

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Layout xmlns="http://soap.sforce.com/2006/04/metadata">
    <!--
        AUTO-GENERATED by Page Layout Accelerator (Layer 4: Generation).
        Source layout: {object_name} / {layout_name}
        Contains Direct + Map fields/related lists/quick actions with
        action=auto_generate ONLY. See the companion build_report JSON for
        everything deliberately NOT included (buttons, retired items,
        decision-needed items, unconfirmed related lists) — nothing was
        silently dropped.
    -->
{chr(10).join(section_blocks)}
{mini_block}{standard_actions}{qa_block}{"".join(rl_blocks)}
    <showEmailCheckbox>false</showEmailCheckbox>
    <showHighlightsPanel>true</showHighlightsPanel>
    <showInteractionLogPanel>false</showInteractionLogPanel>
    <showRunAssignmentRulesCheckbox>false</showRunAssignmentRulesCheckbox>
    <showSubmitAndAttachButton>false</showSubmitAndAttachButton>
</Layout>
"""
    return xml


def main():
    ap = argparse.ArgumentParser(description="Layer 4: generate Flexipage + Page Layout XML from classified elements.")
    ap.add_argument("--object", required=True)
    ap.add_argument("--layout", required=True)
    ap.add_argument("--classified", required=True, help="Path to classified_<layout>.json from rules_engine.py")
    ap.add_argument("--outdir", default=".", help="Output directory")
    args = ap.parse_args()

    with open(args.classified, encoding="utf-8") as f:
        data = json.load(f)
    elements = data["elements"]

    section_order, sections_by_col = group_fields_by_section(elements)

    os.makedirs(args.outdir, exist_ok=True)

    flexipage_xml = gen_flexipage(section_order, sections_by_col, args.object, args.layout)
    layout_xml = gen_page_layout(section_order, sections_by_col, elements, args.object, args.layout)
    report = build_report(elements)

    flexipage_path = os.path.join(args.outdir, f"{args.object}_{args.layout}_Generated.flexipage-meta.xml")
    layout_path = os.path.join(args.outdir, f"{args.object}-{args.layout}_Generated.layout-meta.xml")
    report_path = os.path.join(args.outdir, f"{args.layout}_build_report.json")

    with open(flexipage_path, "w", encoding="utf-8") as f:
        f.write(flexipage_xml)
    with open(layout_path, "w", encoding="utf-8") as f:
        f.write(layout_xml)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    total_fields = sum(len(v) for cols in sections_by_col.values() for v in cols.values())
    print(f"Generated Flexipage: {flexipage_path}")
    print(f"Generated Page Layout: {layout_path}")
    print(f"Sections built: {len(section_order)} | Fields placed: {total_fields}")
    print(f"Build report (deferred items): {report_path}")
    for action, items in report.items():
        print(f"  {action}: {len(items)} item(s)")


if __name__ == "__main__":
    main()
