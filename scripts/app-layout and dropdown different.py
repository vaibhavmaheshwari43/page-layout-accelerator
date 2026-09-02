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

# Auto-computed SFDX project root -- avoids a manual text field a business
# user shouldn't need to understand. Computed from this script's own file
# location: app.py lives in <project>/scripts/, so its parent folder IS
# the real project root, regardless of the terminal's working directory.
AUTO_SFDX_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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


def _select_candidate_callback(full_name, org_alias, sfdx_root):
    """Runs when a candidate's 'Use this one' button is clicked. MUST be a
    callback (on_click=), not inline code after st.button() -- Streamlit
    forbids writing to a widget's session_state key (object_name_input /
    layout_name_input) in the normal script body AFTER that widget has
    already rendered in the same run.

    IMPORTANT: this callback does NOT do the actual (slow) fetch itself.
    Streamlit callbacks can't reliably show UI feedback (like st.spinner)
    during long operations -- the retrieve can take ~1 minute on this org,
    and with no visible feedback during a callback, the screen just looks
    frozen, which is exactly what caused the confusion (having to click
    Fetch again, thinking nothing happened). Instead, this just records
    which candidate was picked; the actual fetch happens in the main
    script body below, where a spinner can actually render."""
    prefix, _, rest = full_name.partition("-")
    st.session_state["object_name_input"] = prefix
    st.session_state["layout_name_input"] = rest
    st.session_state["pending_candidate_fetch"] = (prefix, rest, org_alias, sfdx_root)


def _apply_layout_dropdown_selection():
    """Same safe callback pattern as _select_candidate_callback above --
    must be a callback, not inline code, since the Layout Name text input
    has already rendered earlier in the script by the time this runs."""
    chosen = st.session_state.get("layout_dropdown_choice")
    if chosen:
        st.session_state["layout_name_input"] = chosen


from extractor import extract_all
from rules_engine import classify_elements, link_duplicates, load_registry, registry_key
from generator import group_fields_by_section, gen_flexipage, gen_page_layout, build_report
from validator import (
    parse_layout_fields, parse_flexipage_fields, compare_layouts,
    compare_flexipages, check_no_leaked_flags,
)

st.set_page_config(page_title="Page Layout Accelerator | ProcDNA", layout="wide")

# ProcDNA branding -- logo only, no wordmark text. Real logo embedded
# directly as base64. Note: CSS braces below are DOUBLED ({{ }}) since
# this whole block is an f-string in app.py itself -- confirmed this
# exact construction actually RUNS correctly, not just parses.
_PROCDNA_LOGO_B64 = "PHN2ZyB3aWR0aD0iNDQzIiBoZWlnaHQ9IjI1NCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIiB4bWxuczp4bGluaz0iaHR0cDovL3d3dy53My5vcmcvMTk5OS94bGluayIgeG1sOnNwYWNlPSJwcmVzZXJ2ZSIgb3ZlcmZsb3c9ImhpZGRlbiI+PGRlZnM+PGNsaXBQYXRoIGlkPSJjbGlwMCI+PHJlY3QgeD0iMTIxIiB5PSIzMzQiIHdpZHRoPSI0NDMiIGhlaWdodD0iMjU0Ii8+PC9jbGlwUGF0aD48bGluZWFyR3JhZGllbnQgeDE9IjEyMSIgeTE9IjQ2MSIgeDI9IjU2NCIgeTI9IjQ2MSIgZ3JhZGllbnRVbml0cz0idXNlclNwYWNlT25Vc2UiIHNwcmVhZE1ldGhvZD0icmVmbGVjdCIgaWQ9ImZpbGwxIj48c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiMwMDFFOTYiLz48c3RvcCBvZmZzZXQ9IjAuNSIgc3RvcC1jb2xvcj0iIzAwNUNEOSIvPjxzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0iIzAwOENFMyIvPjwvbGluZWFyR3JhZGllbnQ+PC9kZWZzPjxnIGNsaXAtcGF0aD0idXJsKCNjbGlwMCkiIHRyYW5zZm9ybT0idHJhbnNsYXRlKC0xMjEgLTMzNCkiPjxwYXRoIGQ9Ik0zNDcuMjIgMzYzLjAwNUMzNDQuMjMyIDM2NC42MTIgMzQzLjExNSAzNjguMzMgMzQ0LjcyMyAzNzEuMzFMMzQ0LjcyIDM3MS4zMDdDMzQ0Ljg5NyAzNzEuNjM2IDM0NS4xMTEgMzcxLjkzNiAzNDUuMzM2IDM3Mi4yMTggMzQyLjEyNyAzNzUuMjY2IDMzOS4xOCAzNzguNjYxIDMzNi41NjQgMzgyLjQ3MyAzMzYuNTY0IDM4Mi40NzMgMzU2LjczOSAyODMuOTY4IDUxNy4yIDM2Ny44MDMgNTE3LjIgMzY3LjgwMyA0NjAuNTkzIDM0Ny4wMyA0MDguNDY4IDM0OS41NzggNDA5LjM3NCAzNDcuMTIyIDQwOS4yOTYgMzQ0LjMxOCA0MDcuOTUzIDM0MS44MyA0MDUuNDcgMzM3LjIyOSAzOTkuNzEzIDMzNS41MDYgMzk1LjA5NyAzMzcuOTgzIDM5MC40ODMgMzQwLjQ2MSAzODguNzU2IDM0Ni4yMDQgMzkxLjI0IDM1MC44MDkgMzkxLjI3OSAzNTAuODgxIDM5MS4zMjMgMzUwLjk0OCAzOTEuMzcgMzUxLjAxMyAzOTEuMzkxIDM1MS4wNDIgMzkxLjQxMiAzNTEuMDcxIDM5MS40MzQgMzUxLjEwMUwzOTEuNDM4IDM1MS4xMDZDMzkxLjQ4OCAzNTEuMTc0IDM5MS41MzkgMzUxLjI0MyAzOTEuNTg0IDM1MS4zMTUgMzc4LjIwMiAzNTMuNDg2IDM2NS42MjEgMzU3LjY2MyAzNTQuOTU3IDM2NC42MjkgMzUzLjE0OCAzNjIuMzIyIDM0OS44OTUgMzYxLjU2NyAzNDcuMjIgMzYzLjAwNVpNNDI5Ljk1NSA0MTQuNjY1QzQzMS40NjEgNDE3LjQ1OSA0MzQuOTUzIDQxOC41MDIgNDM3Ljc1MyA0MTcgNDQwLjU1MyA0MTUuNDk4IDQ0MS42IDQxMi4wMTUgNDQwLjA5NCA0MDkuMjIxIDQzOS4wNDggNDA3LjI4NCA0MzcuMDQ4IDQwNi4yMTcgNDM0Ljk5MSA0MDYuMjI3TDQwNS41OTYgMzUxLjc3QzQwNy44NTggMzQ5LjI5MyA0MDguNDU1IDM0NS41ODkgNDA2Ljc3MSAzNDIuNDczIDQwNC42MzkgMzM4LjUyNCAzOTkuNzAyIDMzNy4wNDYgMzk1Ljc0NyAzMzkuMTczIDM5MS43ODggMzQxLjMgMzkwLjMwNiAzNDYuMjI0IDM5Mi40MzkgMzUwLjE3MyAzOTQuMTIyIDM1My4yODkgMzk3LjU1IDM1NC44MzIgNDAwLjg2OCAzNTQuMzEyTDQzMC4yNjIgNDA4Ljc2OUM0MjkuMTIgNDEwLjQ3OSA0MjguOTEyIDQxMi43MzIgNDI5Ljk1OSA0MTQuNjY5TDQyOS45NTUgNDE0LjY2NVpNMzgyLjQ3OCA0MjguMzI2QzM3OS4xODMgNDMwLjA5NyAzNzUuMDcxIDQyOC44NjYgMzczLjI5NiA0MjUuNTc3IDM3MS44OTMgNDIyLjk3NyAzNzIuMzkzIDQxOS44ODkgMzc0LjI4NCA0MTcuODI2TDM1MC4yMDUgMzczLjIxM0MzNDguNDYxIDM3My4yMzcgMzQ2Ljc2MSAzNzIuMzMzIDM0NS44NzUgMzcwLjY5NSAzNDQuNjA3IDM2OC4zNDcgMzQ1LjQ4NiAzNjUuNDE4IDM0Ny44NDEgMzY0LjE1NCAzNTAuMTk0IDM2Mi44ODkgMzUzLjEzMSAzNjMuNzcgMzU0LjM5OSAzNjYuMTE4IDM1NS4yODQgMzY3Ljc1NiAzNTUuMTA0IDM2OS42NjkgMzU0LjEyMiAzNzEuMTFMMzc4LjIwMiA0MTUuNzIyQzM4MC45NjggNDE1LjI4NCAzODMuODMgNDE2LjU2OCAzODUuMjM0IDQxOS4xNjggMzg3LjAwOSA0MjIuNDU0IDM4NS43NzYgNDI2LjU1NSAzODIuNDc4IDQyOC4zMjZaTTI5NC44MiA0NjYuOTVDMjk2LjI4NSA0NjkuNjcgMjk5LjY4NCA0NzAuNjg2IDMwMi40MDcgNDY5LjIyMSAzMDUuMTMyIDQ2Ny43NiAzMDYuMTUxIDQ2NC4zNjggMzA0LjY4MyA0NjEuNjUzIDMwMy42MTYgNDU5LjY3OCAzMDEuNTI4IDQ1OC42MTkgMjk5LjQyMyA0NTguNzM3TDI4MC42IDQyMy44NjhDMjgyLjMxNCA0MjEuODczIDI4Mi43MzIgNDE4Ljk2NCAyODEuNDA3IDQxNi41MTQgMjc5LjY5IDQxMy4zMjkgMjc1LjcxMSA0MTIuMTQxIDI3Mi41MTkgNDEzLjg1MyAyNjkuMzI3IDQxNS41NjYgMjY4LjEzNCA0MTkuNTM1IDI2OS44NTEgNDIyLjcxOSAyNzEuMTczIDQyNS4xNjkgMjczLjgzNyA0MjYuNDIzIDI3Ni40NSA0MjYuMDk0TDI5NS4yNzMgNDYwLjk2M0MyOTQuMDEyIDQ2Mi42NDkgMjkzLjc1IDQ2NC45NzMgMjk0LjgxNiA0NjYuOTQ4TDI5NC44MiA0NjYuOTVaTTM0Mi45MzEgNTIwLjk5NyAzNzMuMTQzIDUyMC45OTcgMzczLjE0MyA1MjAuOTk0QzM5NC44OTkgNTIwLjk5NCA0MDkuODE0IDUzNC4wNzMgNDA5LjgxNCA1NTQuMTY0IDQwOS44MTQgNTc0LjI1NSAzOTQuODk5IDU4Ny4zMzQgMzczLjE0MyA1ODcuMzM0TDM0Mi45MzEgNTg3LjMzNCAzNDIuOTMxIDUyMC45OTdaTTM1OC4zMjMgNTc0LjczNCAzNzIuMzgzIDU3NC43MzQgMzcyLjM4MyA1NzQuNzM3QzM4NS41ODUgNTc0LjczNyAzOTQuMjI3IDU2Ni44NyAzOTQuMjI3IDU1NC4xNjcgMzk0LjIyNyA1NDEuNDY1IDM4NS41ODUgNTMzLjU5OCAzNzIuMzgzIDUzMy41OThMMzU4LjMyMyA1MzMuNTk4IDM1OC4zMjMgNTc0LjczNFpNMTIxIDUyMC45OTcgMTQ4LjM2IDUyMC45OTdDMTY1Ljg0MSA1MjAuOTk3IDE3Ni42NjggNTMwLjAwOSAxNzYuNjY4IDU0NC41OTggMTc2LjY2OCA1NTkuMTg2IDE2NS44MzcgNTY4LjE4OSAxNDguMzYgNTY4LjE4OUwxMzMuMzQzIDU2OC4xODkgMTMzLjM0MyA1ODcuMzM0IDEyMSA1ODcuMzM0IDEyMSA1MjAuOTk3Wk0xMzMuMzQzIDU1Ny43NjYgMTQ3Ljc5MSA1NTcuNzY2QzE1OC42MTggNTU3Ljc2NiAxNjQuMjIzIDU1Mi45NDQgMTY0LjIyMyA1NDQuNTk4IDE2NC4yMjMgNTM2LjI1MiAxNTguNjE4IDUzMS40MTkgMTQ3Ljc5MSA1MzEuNDE5TDEzMy4zNDMgNTMxLjQxOSAxMzMuMzQzIDU1Ny43NjZaTTE5OC43MTMgNTM2LjczMSAxOTguNzEzIDU0NC4xMTkgMTk4LjcxNyA1NDQuMTE5QzIwMi4xMzcgNTM4LjgyIDIwOC4zMDcgNTM2LjE2MyAyMTYuNzYyIDUzNi4xNjNMMjE2Ljc2MiA1NDcuNDQyQzIxNS43MTMgNTQ3LjI1MiAyMTQuODY5IDU0Ny4xNTMgMjE0LjAxIDU0Ny4xNTMgMjA0Ljk4NSA1NDcuMTUzIDE5OS4yODIgNTUyLjQ2NSAxOTkuMjgyIDU2Mi43ODhMMTk5LjI4MiA1ODcuMzM0IDE4Ny40MDYgNTg3LjMzNCAxODcuNDA2IDUzNi43MzEgMTk4LjcxMyA1MzYuNzMxWk0yNDkuMTU0IDU3Ny44NTdDMjQxLjg0NyA1NzcuODU3IDIzNi4wNjYgNTczLjM1NyAyMzQuNTQ5IDU2Ni4wMUwyMjIuNDQxIDU2Ni4wMUMyMjQuMjU3IDU3OS4wNDYgMjM0Ljk4NSA1ODggMjQ5LjE1NCA1ODggMjYzLjMyMyA1ODggMjc0LjE2NCA1NzkuMDQzIDI3NS45NyA1NjYuMDFMMjYzLjg2MiA1NjYuMDFDMjYyLjM0NiA1NzMuMzU0IDI1Ni41NTQgNTc3Ljg1NyAyNDkuMTU4IDU3Ny44NTdMMjQ5LjE1NCA1NzcuODU3Wk0yMzQuNjA4IDU1Ny43NjYgMjIyLjQ4NiA1NTcuNzY2QzIyNC40MTQgNTQ0Ljg5NyAyMzQuOTA0IDUzNi4xNjMgMjQ5LjE1NCA1MzYuMTYzIDI2My40MDUgNTM2LjE2MyAyNzQuMDExIDU0NC44OTcgMjc1LjkyNiA1NTcuNzY2TDI2My44MDQgNTU3Ljc2NkMyNjIuMTg5IDU1MC41ODUgMjU2LjM2IDU0Ni4yMDkgMjQ5LjE1NCA1NDYuMjA5IDI0MS45NSA1NDYuMjA5IDIzNi4yMjIgNTUwLjU4OSAyMzQuNjA4IDU1Ny43NjZaTTMxMC44MTUgNTQ2LjIwOUMzMTUuODUgNTQ2LjIwOSAzMjAuNDE5IDU0OC4zODcgMzIzLjQ1NyA1NTMuMjE5TDMzMi41NzEgNTQ3LjkwN0MzMjguNjg0IDU0MC4yMjcgMzIwLjc5NyA1MzYuMTYzIDMxMC45MTMgNTM2LjE2MyAyOTQuODYgNTM2LjE2MyAyODMuNDU0IDU0Ni44NjQgMjgzLjQ1NCA1NjIuMDMgMjgzLjQ1NCA1NzcuMTk3IDI5NC44NiA1ODggMzEwLjkxMyA1ODggMzIwLjc5NCA1ODggMzI4LjY4IDU4My43MzIgMzMyLjU3MSA1NzYuMTU0TDMyMy40NTcgNTcwLjg0MkMzMjAuNDE1IDU3NS42NzUgMzE1Ljg1IDU3Ny44NTMgMzEwLjgxNSA1NzcuODUzIDMwMi4wOCA1NzcuODUzIDI5NS40MjkgNTcxLjg4NSAyOTUuNDI5IDU2Mi4wMyAyOTUuNDI5IDU1Mi4xNzYgMzAyLjA4IDU0Ni4yMDkgMzEwLjgxNSA1NDYuMjA5Wk00NjcuMTk1IDU2MS4yNzYgNDM0LjEzMiA1MjAuOTk3IDQyMS40IDUyMC45OTcgNDIxLjQgNTg3LjMzNCA0MzYuNjA1IDU4Ny4zMzQgNDM2LjYwNSA1NDcuMDY1IDQ2OS43NTcgNTg3LjMzNCA0ODIuMzg5IDU4Ny4zMzQgNDgyLjM4OSA1MjAuOTk3IDQ2Ny4xOTUgNTIwLjk5NyA0NjcuMTk1IDU2MS4yNzZaTTUxOS4wNjQgNTIwLjk5NyA1MzQuMjY5IDUyMC45OTcgNTY0IDU4Ny4zMzQgNTQ3Ljg0OSA1ODcuMzM0IDU0MS45NjkgNTczLjEyMyA1MzcuMTIxIDU2MS40NjcgNTM2LjYzIDU2MC4yNzcgNTI2LjU3MyA1MzYuMDY1IDUxNi4wMjIgNTYxLjQ2NyA1MTEuMDk5IDU3My4xMjMgNTEwLjg2NCA1NzMuNjY3IDUwNS4xOTUgNTg3LjMzNCA0ODkuNDMxIDU4Ny4zMzQgNTE5LjA2NCA1MjAuOTk3Wk0zMjUuNTczIDQ2NC41MDRDMzI2LjY5OCA0NjYuNTg0IDMyOS4zIDQ2Ny4zNjYgMzMxLjM4NSA0NjYuMjQ0IDMzMy40NyA0NjUuMTIyIDMzNC4yNTQgNDYyLjUyNiAzMzMuMTMgNDYwLjQ0NyAzMzIuNDk2IDQ1OS4yNzQgMzMxLjM5MiA0NTguNTM3IDMzMC4xNzkgNDU4LjI5NUwzMDkuMTU5IDQxOS4zNTFDMzExLjY1IDQxNi45MjggMzEyLjM3NSA0MTMuMDg4IDMxMC42NDEgNDA5Ljg4MSAzMDguNTQ2IDQwNiAzMDMuNjk1IDQwNC41NDkgMjk5LjgwNCA0MDYuNjM5IDI5NS45MTQgNDA4LjcyOCAyOTQuNDU5IDQxMy41NjcgMjk2LjU1NCA0MTcuNDQ5IDI5OC4yODUgNDIwLjY1NiAzMDEuODk5IDQyMi4xNjggMzA1LjI5OSA0MjEuNDI4TDMyNi4wMDkgNDU5Ljc5NEMzMjQuOTYgNDYxLjA5NSAzMjQuNzMyIDQ2Mi45NDQgMzI1LjU3NiA0NjQuNTA3TDMyNS41NzMgNDY0LjUwNFpNMjg0LjIgNDkyLjE1NkMyODEuODcxIDQ5My40MDYgMjc4Ljk2MSA0OTIuNTM2IDI3Ny43MDcgNDkwLjIxMkwyNzcuNzA3IDQ5MC4yMTVDMjc2Ljg4MyA0ODguNjg2IDI3Ni45OTIgNDg2LjkxOSAyNzcuODE2IDQ4NS41MjZMMjQ4Ljk5OCA0MzIuMTM5QzI0NS4zNzcgNDMyLjg3MyAyNDEuNTU0IDQzMS4yNDggMjM5LjcxMSA0MjcuODM3IDIzNy40NTkgNDIzLjY2NCAyMzkuMDIzIDQxOC40NjEgMjQzLjIwNiA0MTYuMjE0IDI0Ny4zOSA0MTMuOTY4IDI1Mi42MDYgNDE1LjUyOCAyNTQuODU3IDQxOS43MDEgMjU2LjcwMSA0MjMuMTE0IDI1NS45NTUgNDI3LjE5MSAyNTMuMzQ1IDQyOS44MDFMMjgyLjE2MyA0ODMuMTg3QzI4My43ODYgNDgzLjI2OSAyODUuMzI1IDQ4NC4xNSAyODYuMTQ5IDQ4NS42NzkgMjg3LjQwMyA0ODguMDAzIDI4Ni41MzEgNDkwLjkwNSAyODQuMiA0OTIuMTU2Wk00MTQuNTM5IDQzMi4xNjNDNDEwLjU4MyA0MzQuMjkgNDA1LjY0NyA0MzIuODEyIDQwMy41MTQgNDI4Ljg2M0w0MDMuNTExIDQyOC44NjNDNDAxLjc0NyA0MjUuNTkxIDQwMi40ODkgNDIxLjY3MiA0MDUuMDM3IDQxOS4yMDhMMzgxLjg4MiAzNzYuMzEyQzM4MC4xODEgMzc2LjMyMyAzNzguNTI2IDM3NS40NDIgMzc3LjY2NCAzNzMuODQyIDM3Ni40MjQgMzcxLjUzOCAzNzcuMjg2IDM2OC42NjYgMzc5LjU5NSAzNjcuNDI2IDM4MS45MDUgMzY2LjE4NiAzODQuNzg0IDM2Ny4wNDUgMzg2LjAyOCAzNjkuMzQ5IDM4Ni44OSAzNzAuOTUgMzg2LjcxOSAzNzIuODEyIDM4NS43NzIgMzc0LjIyM0w0MDguOTI5IDQxNy4xMThDNDEyLjM5MyA0MTYuMzU0IDQxNi4wODIgNDE3Ljg5IDQxNy44NDcgNDIxLjE2MyA0MTkuOTggNDI1LjExMSA0MTguNDk4IDQzMC4wMzYgNDE0LjUzOSA0MzIuMTYzWk0yMjguODk4IDQzNi4yNjhDMjI3LjY0NCA0MzMuOTQ0IDIyNC43MzUgNDMzLjA3MyAyMjIuNDA0IDQzNC4zMjQgMjIwLjA3NCA0MzUuNTc1IDIxOS4yMDIgNDM4LjQ3NyAyMjAuNDU1IDQ0MC44MDEgMjIxLjI4NyA0NDIuMzQxIDIyMi44NDQgNDQzLjIyNCAyMjQuNDc2IDQ0My4yOTJMMjQ4LjQ1NiA0ODcuNzIxQzI0Ni42NjggNDg5Ljc5NCAyNDYuMjI4IDQ5Mi44MjUgMjQ3LjYwOCA0OTUuMzc3IDI0OS4zOTMgNDk4LjY4NyAyNTMuNTMzIDQ5OS45MjQgMjU2Ljg1MSA0OTguMTQzIDI2MC4xNjkgNDk2LjM2MyAyNjEuNDA5IDQ5Mi4yMzQgMjU5LjYyNCA0ODguOTI0IDI1OC4yNDcgNDg2LjM3MiAyNTUuNDcxIDQ4NS4wNyAyNTIuNzQ5IDQ4NS40MTdMMjI4Ljc2OCA0NDAuOTg4QzIyOS42MSA0MzkuNTg4IDIyOS43MjkgNDM3LjgwOCAyMjguODk4IDQzNi4yNjhaTTM0Ni4xMyA0NDMuMDk5QzM0Ni4xMyA0NDMuMDk5IDM0MS45MDYgNDUwLjI2MiAzMzIuNzU4IDQ1OC44NDYgMzMzLjE0NyA0NTkuMjA2IDMzMy40OTQgNDU5LjYxOCAzMzMuNzYgNDYwLjEwNyAzMzUuMDcxIDQ2Mi41MzMgMzM0LjE1OSA0NjUuNTYxIDMzMS43MjYgNDY2Ljg2OSAzMjkuNDQzIDQ2OC4wOTYgMzI2LjY1IDQ2Ny4zNTUgMzI1LjIyOSA0NjUuMjU1IDMxNS45MjggNDcyLjQzNiAzMDMuMzE3IDQ3OS42MjkgMjg3LjA1MiA0ODMuODkxIDI4Ny4yODcgNDg0LjE4NyAyODcuNTEyIDQ4NC41IDI4Ny42OTYgNDg0Ljg0MyAyODkuNDEgNDg4LjAyMSAyODguMjIxIDQ5MS45ODIgMjg1LjAzNSA0OTMuNjkyIDI4MS44NSA0OTUuNDAxIDI3Ny44NzcgNDk0LjIxNSAyNzYuMTY0IDQ5MS4wMzggMjc1LjM0MyA0ODkuNTE1IDI3NS4yMDMgNDg3LjgxNiAyNzUuNjE1IDQ4Ni4yNjYgMjcwLjg1IDQ4Ny4wMTEgMjY1LjgwNyA0ODcuNDg3IDI2MC41MDcgNDg3LjY2NiAyNjAuNjAxIDQ4Ny44MTMgMjYwLjcxIDQ4Ny45NDggMjYwLjgwNiA0ODguMDk0IDI2MC44MzcgNDg4LjE0MSAyNjAuODY3IDQ4OC4xOSAyNjAuODk0IDQ4OC4yNDEgMjYzLjA1OCA0OTIuMjUxIDI2MS41NTYgNDk3LjI1IDI1Ny41MzYgNDk5LjQwOCAyNTMuNTE1IDUwMS41NjUgMjQ4LjUwNCA1MDAuMDY3IDI0Ni4zNCA0OTYuMDU3IDI0NC44MTUgNDkzLjIyNiAyNDUuMTQ1IDQ4OS45MiAyNDYuODY5IDQ4Ny40NDkgMjI0Ljc1MiA0ODYuMDMyIDE5OC41NCA0NzkuMzY1IDE2Ny44IDQ2NC4yMjkgMTY3LjggNDY0LjIyOSAyOTIuMjYxIDU2NC40MzcgMzQ2LjEzNCA0NDMuMDkyTDM0Ni4xMyA0NDMuMDk5Wk0yMTkuNTUzIDQ0MS4yODdDMjIwLjExNSA0NDIuMzI3IDIyMC45NTMgNDQzLjEyMiAyMjEuOTI0IDQ0My42NDJMMjIxLjkxNyA0NDMuNjQ1QzIxNC45ODcgNDUwLjE3NyAyMTEuNzQxIDQ1NS40NDEgMjExLjc0MSA0NTUuNDQxIDIxNS4zNjYgNDMzLjI3NyAyMzAuNjQ2IDQxOC45MzQgMjQ4LjMzIDQwOS42NjMgMjc3LjEwMSAzOTQuNTc4IDMxMS40MzggMzk0LjE1IDM0MS4xODcgNDA3LjIxIDM1Mi4yNjYgNDEyLjA3MiAzNjIuNjkxIDQxNS40MTMgMzcyLjQ2NCA0MTcuNTU0IDM3MC42ODQgNDIwLjA1OCAzNzAuMzMyIDQyMy40MzYgMzcxLjg5MyA0MjYuMzI1IDM3NC4wODMgNDMwLjM4MiAzNzkuMTU5IDQzMS45MDEgMzgzLjIyNyA0MjkuNzE2IDM4Ni43ODcgNDI3LjgwMyAzODguMzk5IDQyMy42ODQgMzg3LjI3NSA0MTkuOTY2IDM5Mi40MDkgNDIwLjUxIDM5Ny4zMzggNDIwLjcyNyA0MDIuMDQ2IDQyMC42NTMgNDAwLjY2NyA0MjMuMzc1IDQwMC41ODggNDI2LjcwMiA0MDIuMTQ4IDQyOS41OTQgNDA0LjY4NyA0MzQuMjk0IDQxMC41NiA0MzYuMDU0IDQxNS4yNzIgNDMzLjUyMyA0MTkuOTgzIDQzMC45OSA0MjEuNzQ4IDQyNS4xMzIgNDE5LjIxIDQyMC40MzIgNDE4Ljk2OCA0MTkuOTg3IDQxOC42NjggNDE5LjU5MyA0MTguMzcyIDQxOS4xOTkgNDIyLjM0MSA0MTguNTQ5IDQyNi4xMzYgNDE3LjczMSA0MjkuNzEzIDQxNi43NDEgNDMxLjg4IDQxOS4xMjQgNDM1LjQ1NyA0MTkuODQ0IDQzOC40MjUgNDE4LjI1IDQ0MC42NyA0MTcuMDQ3IDQ0MS45OTQgNDE0LjgxOSA0NDIuMTY5IDQxMi40NTYgNDY1LjUyMyA0MDIuNzUxIDQ3Ny41NzMgMzg3LjkwNyA0NzcuNTczIDM4Ny45MDcgNDc2LjgwNiAzODkuMTU1IDQ3Ni4wMjkgMzkwLjM3NCA0NzUuMjQ5IDM5MS41NzEgNDQ3LjY4NCA0MzMuNzM5IDM5My41MDIgNDUwLjAxNyAzNDYuNjUyIDQzMS4yOTkgMzMzLjMxIDQyNS45NzEgMzIxLjA5IDQyMi41ODMgMzA5LjkxOSA0MjAuNjg3IDMxMy4xMDcgNDE3LjgzNiAzMTQuMDcyIDQxMy4wOTggMzExLjk1MyA0MDkuMTc0IDMwOS40NjkgNDA0LjU3MiAzMDMuNzE1IDQwMi44NDkgMjk5LjA5OSA0MDUuMzI3IDI5NC40ODIgNDA3LjgwNCAyOTIuNzU5IDQxMy41NDQgMjk1LjI0MiA0MTguMTQ4IDI5NS40MTMgNDE4LjQ2NSAyOTUuNjM0IDQxOC43MzYgMjk1LjgzNSA0MTkuMDI1IDI5MS41NzMgNDE4Ljc1IDI4Ny41MDggNDE4LjcyNiAyODMuNjAxIDQxOC44NzMgMjgzLjUwMiA0MTcuODM5IDI4My4yMDIgNDE2LjgwNiAyODIuNjc4IDQxNS44MzQgMjgwLjU4MyA0MTEuOTUzIDI3NS43MzIgNDEwLjUwMyAyNzEuODQxIDQxMi41OTIgMjY4LjgzNiA0MTQuMjA2IDI2Ny4zMDYgNDE3LjQ1OSAyNjcuNzIyIDQyMC42NDYgMjY0LjI1NCA0MjEuMzA2IDI2MC45NyA0MjIuMTI4IDI1Ny44NDYgNDIzLjA2NSAyNTcuNzQ2IDQyMS41OTggMjU3LjM2OSA0MjAuMTI5IDI1Ni42MjIgNDE4Ljc1IDI1My44NDYgNDEzLjYwOSAyNDcuNDE3IDQxMS42ODIgMjQyLjI2MiA0MTQuNDUxIDIzNy4xMDggNDE3LjIyMSAyMzUuMTc3IDQyMy42MzMgMjM3Ljk1MyA0MjguNzc1IDIzOC4zNjUgNDI5LjU0MiAyMzguODgzIDQzMC4yMTIgMjM5LjQ0MiA0MzAuODMxIDIzNi4wMjUgNDMyLjczNyAyMzIuOTU5IDQzNC43NDkgMjMwLjIwNiA0MzYuNzY0IDIzMC4wOTcgNDM2LjQzNSAyMjkuOTY3IDQzNi4xMDQgMjI5Ljc5NyA0MzUuNzg1IDIyOC4yNzQgNDMyLjk2NSAyMjQuNzQ1IDQzMS45MDggMjIxLjkxNyA0MzMuNDI3IDIxOS4wODkgNDM0Ljk0NiAyMTguMDMgNDM4LjQ2NyAyMTkuNTUzIDQ0MS4yODdaIiBmaWxsPSJ1cmwoI2ZpbGwxKSIgZmlsbC1ydWxlPSJldmVub2RkIi8+PC9nPjwvc3ZnPg=="

st.markdown(f"""
<style>
.procdna-header {{
    display: flex;
    align-items: center;
    padding-bottom: 8px;
    border-bottom: 3px solid #005CD9;
    margin-bottom: 16px;
}}
</style>
<div class="procdna-header">
    <img src="data:image/svg+xml;base64,{_PROCDNA_LOGO_B64}" height="40">
</div>
""", unsafe_allow_html=True)

st.title("Veeva \u2192 LSC Page Layout Accelerator")
st.caption("Build: 2026-08-26-v2 (includes: object-Id-to-name resolution, "
          "300s timeouts, always-show-candidates, generic layout search)")
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
st.header("1. Extract")
with st.expander("Upload files & run extraction", expanded=(st.session_state.classified is None)):
    col1, col2 = st.columns(2)

    # NOTE: col2's code runs FIRST here, even though col1 still renders
    # visually on the LEFT and col2 on the RIGHT -- st.columns() already
    # reserved their screen positions above, so code order and visual
    # order are independent. This lets col1's "Load layouts" button (which
    # needs veeva_org_alias) safely use a variable that's visually shown
    # in col2, without a NameError -- Python just needs it assigned
    # earlier in execution order, regardless of which column it appears in.
    with col2:
        veeva_org_alias = st.text_input("Veeva org name", value="veevaSource")
        input_mode = st.radio("Veeva layout source", ["Upload file", "Fetch directly from Veeva org"], horizontal=True)
        fetch_clicked = False
        if input_mode == "Upload file":
            veeva_file = st.file_uploader("Veeva layout XML", type=["xml"])
            veeva_sfdx_root = AUTO_SFDX_ROOT
        else:
            veeva_file = None
            veeva_sfdx_root = AUTO_SFDX_ROOT  # no visible override -- kept simple, per request
            fetch_clicked = st.button("Fetch layout from Veeva org")

        # Optional target-org describe upload -- commented out per
        # earlier request, not deleted, so it can be re-enabled later.
        # describe_file = st.file_uploader(
        #     "Optional: target org's sobject describe JSON (from 'sf sobject describe --json') "
        #     "\u2014 verifies flagged fields against what actually exists in the org",
        #     type=["json"], key="describe_upload")
        describe_file = None

    with col1:
        object_name = st.text_input("Veeva Object", value="Account", key="object_name_input")

        # "Load layouts" now aligned directly with Object Name, since the
        # org alias field moved to the right column.
        load_layouts_clicked = st.button("Load layouts for this object")
        if load_layouts_clicked:
            from veeva_fetch import list_layout_names_for_object, resolve_sf_executable as _resolve_sf
            sf_exe = _resolve_sf()
            with st.spinner(f"Looking up layouts for '{object_name}'..."):
                names = list_layout_names_for_object(sf_exe, object_name, veeva_org_alias, AUTO_SFDX_ROOT)
            st.session_state["available_layouts_for_object"] = names

        available = st.session_state.get("available_layouts_for_object")
        if available is not None:
            if available:
                st.selectbox(
                    f"{len(available)} layout(s) found on '{object_name}' \u2014 pick one",
                    options=available,
                    key="layout_dropdown_choice",
                    on_change=_apply_layout_dropdown_selection,
                )
            else:
                st.warning(f"No layouts found for '{object_name}' \u2014 double-check the object name is correct.")

        layout_name = st.text_input("Layout Name", value="SP_Admin_Layout_HCO", key="layout_name_input")

    if input_mode == "Fetch directly from Veeva org" and fetch_clicked:
        from veeva_fetch import fetch_veeva_layout
        with st.spinner(f"Looking for {object_name}/{layout_name} in {veeva_org_alias}... "
                        f"this can take a minute or two on this org, please wait."):
            fetch_result = fetch_veeva_layout(object_name, layout_name, veeva_org_alias, veeva_sfdx_root)
        st.session_state.veeva_fetch_result = fetch_result

    # Handles the "Use this one" candidate selection -- the actual slow
    # fetch happens HERE (not in the callback above), specifically so a
    # real spinner can render during the ~1 minute wait, instead of the
    # screen looking frozen with no explanation.
    #
    # IMPORTANT: calls try_direct_retrieve directly, NOT the full
    # fetch_veeva_layout search flow. Once a specific candidate has been
    # explicitly selected, there is zero ambiguity left -- re-running the
    # whole fuzzy-search machinery here was a real bug: if the retrieval
    # failed for any reason, it would silently fall back into ANOTHER
    # ambiguous search, which could produce another candidate needing
    # another click, forever -- exactly the infinite loop reported. A
    # confirmed selection should only ever succeed or show a clear error,
    # never loop back into search.
    if st.session_state.get("pending_candidate_fetch"):
        prefix, rest, pending_org_alias, pending_sfdx_root = st.session_state.pop("pending_candidate_fetch")
        from veeva_fetch import try_direct_retrieve, resolve_sf_executable
        sf_exe = resolve_sf_executable()
        with st.spinner(f"Fetching confirmed layout '{prefix}-{rest}'... this can take a minute or two on this org, please wait."):
            found_name, direct_error = try_direct_retrieve(sf_exe, prefix, rest, pending_org_alias, pending_sfdx_root)
        if found_name:
            expected_path = os.path.join(pending_sfdx_root, "force-app", "main", "default",
                                          "layouts", f"{found_name}.layout-meta.xml")
            fetch_result = {
                "status": "FOUND_DIRECT",
                "layout_full_name": found_name,
                "file_path": expected_path,
                "message": f"Confirmed and retrieved '{found_name}'.",
            }
        else:
            fetch_result = {
                "status": "ERROR",
                "message": (f"Selected '{prefix}-{rest}', but retrieval failed -- this candidate's "
                           f"name may not have resolved correctly. Raw error: {direct_error}"),
            }
        st.session_state.veeva_fetch_result = fetch_result

    if st.session_state.get("veeva_fetch_result"):
        fr = st.session_state.veeva_fetch_result
        if fr["status"] in ("FOUND_DIRECT", "FOUND_FUZZY"):
            st.success(f"\u2713 {fr['message']}")
            if fr["status"] == "FOUND_FUZZY":
                assignments = fr.get("profile_assignments") or []
                if assignments:
                    assignment_str = ", ".join(
                        f"{a['profile']}" + (f" ({a['record_type']})" if a.get("record_type") else "")
                        for a in assignments
                    )
                    st.caption(f"\U0001F464 Assigned to: {assignment_str} \u2014 confirm this looks right "
                              f"before proceeding, since this was an auto-matched fuzzy result, not an exact name.")
                else:
                    st.caption("\u26a0\ufe0f No profile assignment info available for this match \u2014 "
                              "since this was auto-matched (not an exact name), consider double-checking "
                              "this is the right layout before proceeding.")
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
            for i, c in enumerate(fr["candidates"]):
                assignments = c.get("profile_assignments") or []
                if assignments:
                    assignment_str = ", ".join(
                        f"{a['profile']}" + (f" ({a['record_type']})" if a.get("record_type") else "")
                        for a in assignments
                    )
                else:
                    assignment_str = "no profile assignment info available"

                cand_col1, cand_col2 = st.columns([5, 1])
                with cand_col1:
                    st.write(f"**{c['full_name']}** (similarity {c['similarity']}) \u2014 assigned to: {assignment_str}")
                with cand_col2:
                    st.button(
                        "Use this one",
                        key=f"select_candidate_{i}",
                        on_click=_select_candidate_callback,
                        args=(c["full_name"], veeva_org_alias, veeva_sfdx_root),
                    )
            st.caption("Click \"Use this one\" next to the correct layout, or adjust the Object/Layout Name fields above manually.")
        else:
            st.error(fr["message"])

# Mapping Registry upload -- deliberately OUTSIDE the "Extract" expander
# above. It's not something you extract from Veeva, it's the reference
# data used to REVIEW what was extracted -- so it gets its own clearly
# separate area, conceptually feeding into Step 2, even though it still
# has to be provided before clicking Run below (extraction and
# classification happen together, in one action).
st.subheader("Mapping Registry (used for Review & Approve)")
registry_file = st.file_uploader("Mapping Registry JSON (existing)", type=["json"])

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
        registry_index, registry_conflicts, specific_index = load_registry(registry_path)
        classified = classify_elements(raw_elements, registry_index, object_name, specific_index)
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
    informational = sum(1 for e in classified if e.get("registry_match") is None)

    summary_msg = f"Extracted {len(classified)} elements \u2014 {matched} matched the registry, {unmatched} did not"
    if informational:
        summary_msg += f", {informational} informational note(s) (not a real layout component, no action needed)"
    summary_msg += "."
    st.success(summary_msg)

    if st.session_state.registry_conflicts:
        st.warning(f"\u26a0\ufe0f The reference data (registry) has {len(st.session_state.registry_conflicts)} "
                   f"field(s) with disagreeing instructions stored \u2014 needs a decision on which is correct "
                   f"before those specific fields can be fully trusted.")
        for c in st.session_state.registry_conflicts:
            field_name = c["key"][1]
            st.markdown(f"**{field_name}** \u2014 recorded differently in different places:")
            for section, classification, action in zip(
                    c["sections_involved"], c["conflicting_classifications"], c["conflicting_actions"]):
                st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;\u2022 In *\"{section}\"*: says **{classification}** ({action})")

    if st.session_state.duplicate_conflicts:
        st.warning(f"\u26a0\ufe0f {len(st.session_state.duplicate_conflicts)} element(s) appear in multiple "
                   f"locations on THIS layout with DIFFERENT actions \u2014 review these before approving.")
        for c in st.session_state.duplicate_conflicts:
            element_name = c["element"][1] if isinstance(c.get("element"), (list, tuple)) else c.get("element", "Unknown")
            locations = ", ".join(c.get("locations", []))
            actions = ", ".join(c.get("conflicting_actions", []))
            st.markdown(f"**{element_name}** \u2014 appears in: *{locations}*, with disagreeing actions: **{actions}**")

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

        # Plain-English recommended action, separate from our internal
        # action code -- so a reviewer doesn't need to decode
        # 'flag_decision_needed' to understand what's actually being asked.
        action_code = e.get("action", "-")
        target_field = e.get("target", {}).get("api_name")
        target_obj = e.get("target", {}).get("object")
        if action_code == "auto_generate" and target_field:
            recommended = f"Build automatically \u2192 {target_field}" + (f" on {target_obj}" if target_obj else "")
        elif action_code == "flag_manual_review":
            recommended = f"Confirm mapping to {target_field}" + (f" on {target_obj}" if target_obj else "") + ", then build"
        elif action_code == "flag_rebuild":
            recommended = "Needs a custom rebuild \u2014 no direct LSC equivalent"
        elif action_code == "flag_retire":
            recommended = "Candidate for removal \u2014 needs sign-off before dropping"
            if e.get("behavior") == "Required":
                recommended = "\u26a0\ufe0f SUSPICIOUS RETIRE: this field is Required in Veeva \u2014 " + recommended
        elif action_code == "flag_decision_needed":
            recommended = "Needs a human decision before proceeding"
        elif action_code == "flag_no_registry_entry":
            recommended = "No mapping known yet \u2014 needs one added to the registry"
        else:
            recommended = "-"

        if action_code == "auto_generate":
            flag = "\u2705"
        elif action_code == "flag_manual_review":
            flag = "\U0001F7E1"
        else:
            flag = "\U0001F534"

        rows.append({
            "idx": i,
            "\u26a0": flag,
            "Section": e["layout_section"],
            "Type": e["element_type"],
            "Veeva API Name": e["api_name"],
            "Veeva Object": e.get("veeva_object") or "-",
            "Veeva Label": e.get("veeva_label") or "-",
            "Recommended Action": recommended,
            "Basis": e.get("basis", "-"),
            "Datatype": e.get("veeva_datatype") or "-",
            "Behavior": e.get("behavior", "-"),
            "Classification": e.get("classification", {}).get("canonical", "-"),
            "Target Object": target_obj or "-",
            "Target Field API Name": target_field or "-",
            "Confidence": e.get("target", {}).get("confidence", "-"),
            "Org Verified": "\u2713" if e.get("org_verified") else ("\u2717" if "org_verified" in e else "-"),
            "Candidates (from org)": candidates_str,
            "Status (system)": action_code,
            "Reviewer Comment": "",
        })
    df = pd.DataFrame(rows)
    flag_priority = {"\U0001F534": 0, "\U0001F7E1": 1, "\u2705": 2}
    df["_sort_priority"] = df["\u26a0"].map(flag_priority)
    df = df.sort_values("_sort_priority", kind="stable").drop(columns=["_sort_priority"]).reset_index(drop=True)

    red_count = (df["\u26a0"] == "\U0001F534").sum()
    yellow_count = (df["\u26a0"] == "\U0001F7E1").sum()
    green_count = (df["\u26a0"] == "\u2705").sum()
    st.markdown(f"""
<div style="background-color:#F0F4FA; border:1px solid #005CD9; border-radius:6px;
            padding:14px 18px; color:#001E96; margin-bottom:12px;">
<b>How to read this table:</b> \u2705 <b>{green_count} will be built automatically</b> \u2014
nothing to do. \U0001F7E1 <b>{yellow_count} need a quick confirmation</b> \u2014 the answer is
proposed, just needs a yes. \U0001F534 <b>{red_count} need your decision</b> \u2014 rows are
sorted so these appear first.
<br><br>
<b>What the terms mean:</b><br>
&bull; <b>Direct</b> \u2014 a standard field that's identical on both systems. Nothing was decided, it just carries over.<br>
&bull; <b>Map</b> \u2014 a specific field where we've confirmed exactly what it becomes (same name or different).<br>
&bull; <b>Rebuild</b> \u2014 this should still exist, but the old way it worked can't carry over directly \u2014 needs custom development.<br>
&bull; <b>Retire</b> \u2014 this might not be needed anymore \u2014 flagged for you to confirm: keep it, or drop it?<br>
&bull; <b>Decision Needed</b> \u2014 genuinely unclear, needs your judgment before we can proceed.
</div>
""", unsafe_allow_html=True)

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
                df.loc[df["idx"] == i, "Status (system)"] = "auto_generate"
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
            "Status (system)": st.column_config.SelectboxColumn(options=action_options),
            "Recommended Action": st.column_config.TextColumn(width=280),
            "Basis": st.column_config.TextColumn(width=420),
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
    #
    # IMPORTANT: look up by preserved 'idx', not row position — the table
    # is sorted (needs-attention rows first).
    approved_by_idx = {row["idx"]: row for _, row in approved.iterrows()}
    reviewed_elements = []
    for i, e in enumerate(st.session_state.classified):
        row = approved_by_idx[i]
        e2 = dict(e)
        e2["action"] = row["Status (system)"]
        e2["target"] = {**e.get("target", {}),
                        "api_name": row["Target Field API Name"] if row["Target Field API Name"] != "-" else None,
                        "object": row["Target Object"] if row["Target Object"] != "-" else e.get("target", {}).get("object")}
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
            sfdx_root = AUTO_SFDX_ROOT
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
                        result = subprocess.run(cmd, cwd=sfdx_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
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
                    result = subprocess.run(cmd, cwd=sfdx_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
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
                    result = subprocess.run(retrieve_cmd, cwd=sfdx_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
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
