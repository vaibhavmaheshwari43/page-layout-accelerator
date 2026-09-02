#!/usr/bin/env python3
"""
Veeva -> LSC Page Layout Accelerator — Auto-Fetch

Implements the "just give it an object + layout name" request: instead of
manually exporting the Veeva layout XML and hunting down its exact API name
(the Account vs PersonAccount prefix mismatch we hit by hand with HCP),
this script does that automatically.

Strategy:
  1. Try the obvious guess first: "<object>-<layout>" (works for most cases,
     e.g. Account-SP_Admin_Layout_HCO).
  2. If that fails, list every Layout in the org and fuzzy-match by object
     prefix variants (Account, PersonAccount, etc. -- Salesforce sometimes
     uses a different object prefix for the same underlying object) and by
     layout name similarity. Never guesses silently -- if there's a unique
     strong match, use it; if there's ambiguity, report the candidates and
     let the person choose, rather than picking wrong.

Requires the Salesforce CLI ('sf') already authenticated to the Veeva org
(same 'sf org login web' step used earlier in this project).

Usage:
    python3 veeva_fetch.py --object Account --layout SP_Admin_Layout_HCO \
        --org veevaSource --sfdx-root .

    python3 veeva_fetch.py --object PersonAccount --layout SP_Admin_Layout \
        --org veevaSource --sfdx-root .
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from difflib import SequenceMatcher


def get_profile_assignments(sf_exe, layout_full_name, org_alias, sfdx_root):
    """Looks up which profiles (and record types) a layout is actually
    assigned to, via the Tooling API's ProfileLayout object.

    Confirmed against a real org that this needs TWO queries, not one:
    1. Layout.Name does NOT include the object prefix (e.g. it's just
       'SP_Admin_Layout_HCO', not 'Account-SP_Admin_Layout_HCO') -- so we
       first look up the Layout's real Id using just the name portion after
       the object prefix.
    2. ProfileLayout links via LayoutId, not by name/relationship directly --
       so the second query filters on that Id.

    Best-effort throughout: any failure degrades to an empty list rather
    than breaking the fetch entirely.
    """
    # Strip the object prefix -- Layout.Name is only the part after the
    # first '-' (e.g. "Account-SP_Admin_Layout_HCO" -> "SP_Admin_Layout_HCO").
    bare_name = layout_full_name.split("-", 1)[1] if "-" in layout_full_name else layout_full_name

    try:
        layout_query = f"SELECT Id FROM Layout WHERE Name = '{bare_name}'"
        cmd = [sf_exe, "data", "query", "--use-tooling-api",
               "--query", layout_query, "--target-org", org_alias, "--json"]
        result = subprocess.run(cmd, cwd=sfdx_root, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=60)
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        layout_records = data.get("result", {}).get("records", [])
        if not layout_records:
            return []
        # A bare name (without object prefix) could theoretically match more
        # than one layout across different objects -- check all of them
        # rather than assuming the first is correct.
        layout_ids = [r["Id"] for r in layout_records]

        assignments = []
        for layout_id in layout_ids:
            pl_query = (f"SELECT Profile.Name, RecordType.Name FROM ProfileLayout "
                       f"WHERE LayoutId = '{layout_id}'")
            cmd2 = [sf_exe, "data", "query", "--use-tooling-api",
                    "--query", pl_query, "--target-org", org_alias, "--json"]
            result2 = subprocess.run(cmd2, cwd=sfdx_root, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=60)
            if result2.returncode != 0:
                continue
            data2 = json.loads(result2.stdout)
            for r in data2.get("result", {}).get("records", []):
                profile_name = (r.get("Profile") or {}).get("Name", "Unknown Profile")
                record_type_name = (r.get("RecordType") or {}).get("Name")
                assignments.append({"profile": profile_name, "record_type": record_type_name})
        return assignments
    except Exception:
        return []


def resolve_sf_executable():
    """Same Windows sf.cmd resolution fix used in app.py -- subprocess needs
    the full resolved path, not a bare 'sf' string, on Windows."""
    resolved = shutil.which("sf")
    return resolved if resolved else "sf"


def try_direct_retrieve(sf_exe, object_name, layout_name, org_alias, sfdx_root):
    """Attempt the straightforward guess: '<object>-<layout>'.

    IMPORTANT: the Salesforce CLI can return a 'success' status (exit code 0,
    JSON status 0) even for a retrieve that matched NOTHING -- an empty,
    no-op success, not a hard error. Trusting that status alone produces a
    false positive (confirmed by testing with a deliberately wrong object
    name). The only reliable proof is checking whether the file actually
    landed on disk."""
    full_name = f"{object_name}-{layout_name}"
    cmd = [sf_exe, "project", "retrieve", "start",
           "--metadata", f"Layout:{full_name}",
           "--target-org", org_alias, "--json"]
    result = subprocess.run(cmd, cwd=sfdx_root, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120)

    expected_path = os.path.join(sfdx_root, "force-app", "main", "default",
                                  "layouts", f"{full_name}.layout-meta.xml")

    if result.returncode == 0:
        try:
            data = json.loads(result.stdout)
            if data.get("status") == 0 and os.path.isfile(expected_path):
                return full_name, None
            elif data.get("status") == 0:
                return None, (f"CLI reported success but no file was created at "
                              f"{expected_path} -- '{full_name}' likely doesn't exist in this org.")
        except json.JSONDecodeError:
            pass
    return None, result.stdout + result.stderr


def list_all_layouts(sf_exe, org_alias, sfdx_root):
    """Lists every Layout in the org, for fuzzy-matching when the direct
    guess fails."""
    cmd = [sf_exe, "org", "list", "metadata",
           "--target-org", org_alias, "--metadata-type", "Layout", "--json"]
    result = subprocess.run(cmd, cwd=sfdx_root, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120)
    if result.returncode != 0:
        return [], result.stderr
    try:
        data = json.loads(result.stdout)
        return data.get("result", []), None
    except json.JSONDecodeError:
        return [], "Could not parse org metadata list as JSON."


# Known object-prefix variants Salesforce uses for the same underlying
# object in different metadata contexts (the exact issue we hit manually:
# HCP's layout was stored as "PersonAccount-...", not "Account-...").
OBJECT_PREFIX_VARIANTS = {
    "account": ["Account", "PersonAccount"],
    "personaccount": ["PersonAccount", "Account"],
}


def find_candidate_layouts(all_layouts, object_name, layout_name, sf_exe=None, org_alias=None, sfdx_root=None):
    """Fuzzy-matches the requested (object, layout) against every real
    layout in the org. Returns a ranked list of candidates -- never a single
    silent guess."""
    prefixes_to_try = OBJECT_PREFIX_VARIANTS.get(object_name.lower(), [object_name])
    candidates = []

    for layout in all_layouts:
        full_name = layout.get("fullName", "")
        if "-" not in full_name:
            continue
        prefix, _, rest = full_name.partition("-")

        prefix_matches = any(prefix.lower() == p.lower() for p in prefixes_to_try)
        name_similarity = SequenceMatcher(None, layout_name.lower(), rest.lower()).ratio()
        if layout_name.lower() == rest.lower():
            name_similarity = 1.0
        elif layout_name.lower() in rest.lower() or rest.lower() in layout_name.lower():
            name_similarity = max(name_similarity, 0.85)

        if prefix_matches and name_similarity >= 0.5:
            candidates.append({
                "full_name": full_name,
                "prefix": prefix,
                "similarity": round(name_similarity, 2),
            })

    candidates.sort(key=lambda c: c["similarity"], reverse=True)

    # Enrich each candidate with real profile assignment info -- a much
    # stronger disambiguation signal than name similarity alone. Best-effort,
    # and only attempted if the caller provided the org connection details;
    # if this fails for any reason, the candidate still shows up, just
    # without assignment info attached.
    if sf_exe and org_alias and sfdx_root is not None:
        for c in candidates[:5]:
            c["profile_assignments"] = get_profile_assignments(sf_exe, c["full_name"], org_alias, sfdx_root)

    return candidates


def fetch_veeva_layout(object_name, layout_name, org_alias, sfdx_root="."):
    """Main entry point. Returns a dict with status and details -- never
    raises on a normal 'not found' case, only on real infrastructure errors
    (sf CLI missing, org not authenticated, etc.)."""
    sf_exe = resolve_sf_executable()

    found_name, direct_error = try_direct_retrieve(sf_exe, object_name, layout_name, org_alias, sfdx_root)
    if found_name:
        expected_path = os.path.join(sfdx_root, "force-app", "main", "default",
                                      "layouts", f"{found_name}.layout-meta.xml")
        return {
            "status": "FOUND_DIRECT",
            "layout_full_name": found_name,
            "file_path": expected_path,
            "message": f"Found and retrieved directly as '{found_name}'.",
        }

    all_layouts, list_error = list_all_layouts(sf_exe, org_alias, sfdx_root)
    if list_error:
        return {
            "status": "ERROR",
            "message": f"Could not list layouts from org '{org_alias}': {list_error}",
        }

    candidates = find_candidate_layouts(all_layouts, object_name, layout_name, sf_exe, org_alias, sfdx_root)

    if not candidates:
        return {
            "status": "NOT_FOUND",
            "message": (f"No layout matching object='{object_name}', layout='{layout_name}' "
                       f"found in org '{org_alias}', even after fuzzy search. "
                       f"Direct attempt error: {direct_error}"),
        }

    top = candidates[0]
    if top["similarity"] >= 0.85 and (len(candidates) == 1 or candidates[1]["similarity"] < 0.7):
        cmd = [sf_exe, "project", "retrieve", "start",
               "--metadata", f"Layout:{top['full_name']}",
               "--target-org", org_alias]
        result = subprocess.run(cmd, cwd=sfdx_root, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=120)
        expected_path = os.path.join(sfdx_root, "force-app", "main", "default",
                                      "layouts", f"{top['full_name']}.layout-meta.xml")
        if result.returncode == 0 and os.path.isfile(expected_path):
            return {
                "status": "FOUND_FUZZY",
                "layout_full_name": top["full_name"],
                "file_path": expected_path,
                "message": (f"Direct name guess failed, but found a unique strong match: "
                           f"'{top['full_name']}' (similarity {top['similarity']}). Retrieved successfully."),
            }
        return {
            "status": "ERROR",
            "message": (f"Found candidate '{top['full_name']}' but retrieval didn't produce a file "
                       f"at {expected_path}. CLI output: {result.stderr or result.stdout}"),
        }

    return {
        "status": "AMBIGUOUS",
        "candidates": candidates[:5],
        "message": "No confident single match. Top candidates found -- please confirm which one.",
    }


def main():
    ap = argparse.ArgumentParser(description="Auto-fetch a Veeva layout by object + layout name.")
    ap.add_argument("--object", required=True, help="e.g. Account or PersonAccount")
    ap.add_argument("--layout", required=True, help="e.g. SP_Admin_Layout_HCO")
    ap.add_argument("--org", required=True, help="Veeva org alias (already authenticated via sf org login web)")
    ap.add_argument("--sfdx-root", default=".", help="Path to the SFDX project root")
    args = ap.parse_args()

    result = fetch_veeva_layout(args.object, args.layout, args.org, args.sfdx_root)

    print(f"Status: {result['status']}")
    print(f"Message: {result['message']}")
    if result["status"] in ("FOUND_DIRECT", "FOUND_FUZZY"):
        print(f"Layout file: {result['file_path']}")
        sys.exit(0)
    elif result["status"] == "AMBIGUOUS":
        print("Candidates:")
        for c in result["candidates"]:
            print(f"  {c['similarity']}  {c['full_name']}")
        sys.exit(2)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
