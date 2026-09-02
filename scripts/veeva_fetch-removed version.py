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


def get_profile_assignments_batch(sf_exe, layout_ids, org_alias, sfdx_root):
    """Same information as get_profile_assignments, but for MANY layouts in
    ONE query instead of one query per layout.

    This is the actual fix for the ~2 minute slowdown: each 'sf' CLI
    invocation has real startup overhead (a few seconds just to spin up the
    process), so looking up 5 candidates individually meant 5+ separate
    calls. A single 'WHERE LayoutId IN (...)' query gets everything at once.

    Returns {layout_id: [assignment, ...]}.
    """
    if not layout_ids:
        return {}
    id_list = ", ".join(f"'{lid}'" for lid in layout_ids)
    query = (f"SELECT LayoutId, Profile.Name, RecordType.Name FROM ProfileLayout "
             f"WHERE LayoutId IN ({id_list})")
    try:
        cmd = [sf_exe, "data", "query", "--use-tooling-api",
               "--query", query, "--target-org", org_alias, "--json"]
        result = subprocess.run(cmd, cwd=sfdx_root, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=60)
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        by_id = {}
        for r in data.get("result", {}).get("records", []):
            lid = r.get("LayoutId")
            profile_name = (r.get("Profile") or {}).get("Name", "Unknown Profile")
            record_type_name = (r.get("RecordType") or {}).get("Name")
            by_id.setdefault(lid, []).append({"profile": profile_name, "record_type": record_type_name})
        return by_id
    except Exception:
        return {}


def get_profile_assignments(sf_exe, layout_full_name, org_alias, sfdx_root):
    """Single-layout version, kept for standalone/manual use. The candidate-
    list path uses get_profile_assignments_batch instead, to avoid the
    per-candidate CLI overhead that caused the slowdown."""
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


def search_layouts_by_name(sf_exe, layout_name, org_alias, sfdx_root):
    """Searches the ENTIRE org for layouts whose name resembles the one
    typed in -- WITHOUT needing to know or guess the object prefix at all.

    This replaces the earlier approach (a hardcoded dict of known
    object-prefix variants, e.g. Account/PersonAccount) which only worked
    for objects we'd already personally hit a naming mismatch on. This tool
    needs to work for ANY object, not just the ones we've manually
    encountered -- hardcoding a dictionary can never scale to that.

    How: split the typed name into tokens (on underscores/spaces) and build
    a SOQL LIKE pattern like '%SP%Admin%Layout%HCO%'. This still finds a
    match even with a missing/extra underscore (the exact bug we hit
    manually), robustly, for any object -- because it's filtered on the
    layout's Name field alone, server-side, not on which object owns it.
    Still fast: this is a targeted query, not a full-org listing -- Salesforce
    only returns rows whose Name matches the pattern, not every layout that
    exists.

    Best-effort: returns None on failure so the caller can fall back to the
    slower full-org listing.
    """
    import re
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", layout_name) if t]
    if not tokens:
        return None
    like_pattern = "%" + "%".join(tokens) + "%"
    # Escape single quotes defensively, even though layout names containing
    # them would be unusual.
    like_pattern_escaped = like_pattern.replace("'", "\\'")

    query = f"SELECT Id, Name, TableEnumOrId FROM Layout WHERE Name LIKE '{like_pattern_escaped}'"
    cmd = [sf_exe, "data", "query", "--use-tooling-api",
           "--query", query, "--target-org", org_alias, "--json"]
    try:
        result = subprocess.run(cmd, cwd=sfdx_root, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=60)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        records = data.get("result", {}).get("records", [])
        return [
            {"fullName": f"{r['TableEnumOrId']}-{r['Name']}", "id": r["Id"]}
            for r in records if r.get("TableEnumOrId") and r.get("Name")
        ]
    except Exception:
        return None


def list_layouts_scoped(sf_exe, object_name, org_alias, sfdx_root):
    """Lists layouts for ONLY the relevant object(s), instead of every
    layout in the entire org. This is the actual fix for the remaining
    slowness -- 'sf org list metadata --metadata-type Layout' fetches every
    layout across every object in the org, which is slow in a Veeva org
    with hundreds of layouts. Filtering via the Tooling API's
    TableEnumOrId field scopes the query server-side instead.

    Includes BOTH known prefix variants (e.g. Account and PersonAccount) in
    one query, so this doesn't accidentally lose the exact kind of
    cross-prefix match this tool was built to catch (HCP's layout was
    stored under PersonAccount, not Account).

    Best-effort: if this query fails for any reason (field not supported,
    permission issue, etc.), returns None so the caller can fall back to
    the slower but guaranteed-correct full listing instead of silently
    returning an incomplete result.
    """
    prefixes_to_try = OBJECT_PREFIX_VARIANTS.get(object_name.lower(), [object_name])
    object_list = ", ".join(f"'{p}'" for p in prefixes_to_try)
    query = f"SELECT Id, Name, TableEnumOrId FROM Layout WHERE TableEnumOrId IN ({object_list})"
    cmd = [sf_exe, "data", "query", "--use-tooling-api",
           "--query", query, "--target-org", org_alias, "--json"]
    try:
        result = subprocess.run(cmd, cwd=sfdx_root, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=60)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        records = data.get("result", {}).get("records", [])
        # Reshape into the same {"fullName": ..., "id": ...} format
        # list_all_layouts produces, so find_candidate_layouts doesn't need
        # to know or care which listing method was used.
        reshaped = [
            {"fullName": f"{r['TableEnumOrId']}-{r['Name']}", "id": r["Id"]}
            for r in records if r.get("TableEnumOrId") and r.get("Name")
        ]
        return reshaped
    except Exception:
        return None


def list_all_layouts(sf_exe, org_alias, sfdx_root):
    """Lists every Layout in the org, for fuzzy-matching when the direct
    guess fails. This is the slow, unscoped fallback -- list_layouts_scoped
    is tried first and is much faster when it works."""
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

def find_candidate_layouts(all_layouts, object_name, layout_name, sf_exe=None, org_alias=None, sfdx_root=None):
    """Ranks the requested (object, layout) against every real layout
    already found by name search. Returns a ranked list of candidates --
    never a single silent guess.

    IMPORTANT: object_name is used as a SOFT scoring boost only, never a
    hard filter. Hard-filtering by a fixed list of known object prefixes
    (the old approach) only worked for objects we'd personally hit a
    mismatch on before -- it can't generalize to arbitrary objects. Since
    candidates arrive here already pre-filtered by name similarity (from
    search_layouts_by_name, which searches the whole org regardless of
    object), a layout under a genuinely unexpected object should still be
    able to surface as a candidate here.
    """
    candidates = []

    for layout in all_layouts:
        full_name = layout.get("fullName", "")
        if "-" not in full_name:
            continue
        prefix, _, rest = full_name.partition("-")

        name_similarity = SequenceMatcher(None, layout_name.lower(), rest.lower()).ratio()
        if layout_name.lower() == rest.lower():
            name_similarity = 1.0
        elif layout_name.lower() in rest.lower() or rest.lower() in layout_name.lower():
            name_similarity = max(name_similarity, 0.85)

        # Soft boost, not a requirement: if the object prefix happens to
        # match what was typed (or a couple of known Account/PersonAccount
        # variants), nudge the score up slightly to help it rank higher
        # among equally name-similar candidates -- but a mismatch here
        # never excludes a candidate outright.
        known_variants = {"account": ["account", "personaccount"], "personaccount": ["personaccount", "account"]}
        soft_prefix_hints = known_variants.get(object_name.lower(), [object_name.lower()])
        if prefix.lower() in soft_prefix_hints:
            name_similarity = min(1.0, name_similarity + 0.05)

        if name_similarity >= 0.5:
            candidates.append({
                "full_name": full_name,
                "prefix": prefix,
                "similarity": round(name_similarity, 2),
                # 'id' is already returned by the search query -- reusing
                # it means we don't need a separate lookup query per
                # candidate later, which was the main source of the
                # profile-lookup slowdown.
                "id": layout.get("id"),
            })

    candidates.sort(key=lambda c: c["similarity"], reverse=True)

    # Enrich the top candidates with real profile assignment info -- a much
    # stronger disambiguation signal than name similarity alone. Best-effort,
    # and only attempted if the caller provided the org connection details.
    # This is now ONE batched query for all candidates, not one query per
    # candidate (the actual fix for the ~2 minute slowdown).
    if sf_exe and org_alias and sfdx_root is not None:
        top_candidates = candidates[:5]
        ids = [c["id"] for c in top_candidates if c.get("id")]
        assignments_by_id = get_profile_assignments_batch(sf_exe, ids, org_alias, sfdx_root)
        for c in top_candidates:
            c["profile_assignments"] = assignments_by_id.get(c.get("id"), [])

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

    # Try the fast, object-agnostic name search FIRST -- works for any
    # object, no hardcoded prefix list needed, and still fast because
    # Salesforce filters by name server-side rather than returning every
    # layout in the org. Falls back to the object-scoped query, then the
    # slow full-org listing, only if the faster methods don't work for some
    # reason -- correctness is never sacrificed for speed.
    all_layouts = search_layouts_by_name(sf_exe, layout_name, org_alias, sfdx_root)
    if not all_layouts:
        all_layouts = list_layouts_scoped(sf_exe, object_name, org_alias, sfdx_root)
    if all_layouts is None:
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

    # Deliberately no auto-confirm shortcut here, even for a single very
    # strong match. Since the direct name guess already failed, whatever
    # was found is by definition NOT an exact match -- always show the
    # candidate(s) and require an explicit "Use this one" choice, rather
    # than silently finalizing a fuzzy result on the person's behalf.
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
