#!/usr/bin/env python3
"""
Veeva -> LSC Page Layout Accelerator — Layout Assignment Automation

Automates the "who actually sees this layout" step. A page layout being
correctly built and deployed doesn't mean anyone sees it -- Salesforce
decides which layout to show per (Profile, Record Type) pair, and that
assignment lives in each Profile's own metadata as a <layoutAssignments>
block. This is real, deployable metadata -- not something that has to be
clicked through by hand in Setup.

How it works:
1. Pull the REAL (Profile, Record Type) pairs that use this layout in the
   Veeva SOURCE org -- reusing the exact same query logic already proven in
   veeva_fetch.py's get_profile_assignments_batch.
2. Generate one Profile metadata XML per profile, assigning the NEW
   generated layout to those same record types in the TARGET org.
3. Deploy those Profile fragments via SFDX (dry-run first, same discipline
   as everything else in this project).

Honest limitation: if the target org uses DIFFERENT profile names than the
source Veeva org, a straight name-for-name copy would be wrong. This script
supports an optional profile-name mapping for exactly that case; without
one, it assumes the names match.

Usage:
    python3 layout_assignment.py \
        --object Account --layout SP_Admin_Layout_HCO \
        --source-org veevaSource --target-org lsc-new-org \
        --sfdx-root . --dry-run
"""

import argparse
import json
import os
import shutil
import subprocess
import sys


def resolve_sf_executable():
    resolved = shutil.which("sf")
    return resolved if resolved else "sf"


def get_source_layout_assignments(sf_exe, object_name, layout_name, source_org_alias, sfdx_root):
    """Pulls the real (Profile, RecordType) pairs assigned to this layout in
    the SOURCE (Veeva) org.

    Requires THREE queries, not two -- confirmed against a real org error:
    'RecordType.DeveloperName' is NOT queryable through the ProfileLayout
    relationship (only 'RecordType.Name' is, which is the display label,
    NOT the API name needed for deployment -- these can be different
    strings, e.g. label 'Hospital' vs DeveloperName 'Hospital_vod'). So:
      1. Find the Layout's Id by its bare name.
      2. Query ProfileLayout by that Id, getting Profile.Name and the raw
         RecordTypeId (not the nested RecordType.DeveloperName, which fails).
      3. Separately query the standard RecordType object directly by Id to
         get the real DeveloperName -- this object supports it fine, the
         restriction was specific to the ProfileLayout relationship path.
    """
    bare_name = layout_name

    layout_query = f"SELECT Id FROM Layout WHERE Name = '{bare_name}'"
    cmd = [sf_exe, "data", "query", "--use-tooling-api",
           "--query", layout_query, "--target-org", source_org_alias, "--json"]
    result = subprocess.run(cmd, cwd=sfdx_root, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
    if result.returncode != 0:
        return [], f"Could not find layout '{bare_name}' in source org: {result.stdout} {result.stderr}"
    data = json.loads(result.stdout)
    records = data.get("result", {}).get("records", [])
    if not records:
        return [], f"Layout '{bare_name}' not found in source org describe."
    layout_id = records[0]["Id"]

    pl_query = (f"SELECT Profile.Name, RecordTypeId FROM ProfileLayout "
               f"WHERE LayoutId = '{layout_id}'")
    cmd2 = [sf_exe, "data", "query", "--use-tooling-api",
            "--query", pl_query, "--target-org", source_org_alias, "--json"]
    result2 = subprocess.run(cmd2, cwd=sfdx_root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
    if result2.returncode != 0:
        return [], f"Could not query ProfileLayout assignments: {result2.stdout} {result2.stderr}"
    data2 = json.loads(result2.stdout)
    raw_pairs = []
    record_type_ids = set()
    for r in data2.get("result", {}).get("records", []):
        profile_name = (r.get("Profile") or {}).get("Name")
        rt_id = r.get("RecordTypeId")
        if profile_name:
            raw_pairs.append({"profile": profile_name, "record_type_id": rt_id})
            if rt_id:
                record_type_ids.add(rt_id)

    # Step 3: resolve RecordTypeId -> real DeveloperName via the standard
    # RecordType object directly (not through ProfileLayout's relationship,
    # which is what failed).
    dev_name_by_id = {}
    record_type_lookup_error = None
    if record_type_ids:
        id_list = ", ".join(f"'{rid}'" for rid in record_type_ids)
        rt_query = f"SELECT Id, DeveloperName FROM RecordType WHERE Id IN ({id_list})"
        # NOTE: dropping --use-tooling-api here specifically. Confirmed
        # against a real org (twice, via two different query paths) that
        # RecordType.DeveloperName is not queryable through the Tooling
        # API in this org at all -- 'No such column DeveloperName on
        # entity RecordType'. RecordType is a standard business object,
        # not Tooling-specific metadata, so querying it through the
        # regular Data API instead is worth trying -- it's a genuinely
        # different data path that may expose fields the Tooling API
        # doesn't here.
        cmd3 = [sf_exe, "data", "query",
                "--query", rt_query, "--target-org", source_org_alias, "--json"]
        result3 = subprocess.run(cmd3, cwd=sfdx_root, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=60)
        if result3.returncode == 0:
            data3 = json.loads(result3.stdout)
            for r in data3.get("result", {}).get("records", []):
                dev_name_by_id[r["Id"]] = r.get("DeveloperName")
        else:
            # This was previously swallowed silently, producing "no record
            # type" for every single row with zero explanation. Surface the
            # real error instead -- degrade gracefully (still return the
            # profile assignments) but make the failure visible, not hidden.
            record_type_lookup_error = f"{result3.stdout} {result3.stderr}"

    assignments = []
    for pair in raw_pairs:
        rt_id = pair["record_type_id"]
        dev_name = dev_name_by_id.get(rt_id) if rt_id else None
        assignments.append({"profile": pair["profile"], "record_type": dev_name})

    if record_type_lookup_error:
        print(f"WARNING: Record Type Id -> DeveloperName lookup failed, so all "
              f"assignments below show 'no record type' even though real record "
              f"type Ids exist. Raw error: {record_type_lookup_error}")

    return assignments, None


def profile_name_to_filename(profile_name):
    """Salesforce Profile metadata files use underscores instead of spaces,
    e.g. 'System Administrator' -> 'System_Administrator'."""
    return profile_name.replace(" ", "_")


def build_profile_assignment_xml(object_name, layout_full_name, record_types_for_profile):
    """Builds a MINIMAL Profile metadata fragment containing only
    layoutAssignments. Salesforce merges partial Profile deploys into the
    existing profile rather than replacing it wholesale -- this should only
    touch the layout assignment, not overwrite other profile settings.
    (Documented Salesforce Metadata API behavior; still worth confirming on
    a real deploy, since profile deploys have historically had edge cases.)
    """
    assignment_blocks = []
    for rt in record_types_for_profile:
        if rt:
            assignment_blocks.append(f'''    <layoutAssignments>
        <layout>{layout_full_name}</layout>
        <recordType>{object_name}.{rt}</recordType>
    </layoutAssignments>''')
        else:
            assignment_blocks.append(f'''    <layoutAssignments>
        <layout>{layout_full_name}</layout>
    </layoutAssignments>''')

    assignments_xml = "\n".join(assignment_blocks)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<Profile xmlns="http://soap.sforce.com/2006/04/metadata">
{assignments_xml}
</Profile>
'''


def generate_assignment_files(object_name, layout_full_name, assignments, outdir, profile_name_map=None):
    """Groups assignments by profile and writes one Profile metadata XML
    per profile. profile_name_map optionally translates source-org profile
    names to target-org profile names, for orgs where they differ."""
    profile_name_map = profile_name_map or {}
    by_profile = {}
    for a in assignments:
        source_profile = a["profile"]
        target_profile = profile_name_map.get(source_profile, source_profile)
        by_profile.setdefault(target_profile, []).append(a["record_type"])

    os.makedirs(outdir, exist_ok=True)
    written_files = []
    for profile_name, record_types in by_profile.items():
        xml = build_profile_assignment_xml(object_name, layout_full_name, record_types)
        filename = f"{profile_name_to_filename(profile_name)}.profile-meta.xml"
        path = os.path.join(outdir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(xml)
        written_files.append(path)
    return written_files


def main():
    ap = argparse.ArgumentParser(description="Recreate layout assignments (Profile + Record Type) in the target org.")
    ap.add_argument("--object", required=True)
    ap.add_argument("--layout", required=True)
    ap.add_argument("--source-org", required=True, help="Veeva org alias to read assignments FROM")
    ap.add_argument("--target-org", required=True, help="LSC org alias to deploy assignments TO")
    ap.add_argument("--sfdx-root", default=".")
    ap.add_argument("--profile-map", default=None,
                    help="Optional JSON file mapping source profile names to target profile names.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sf_exe = resolve_sf_executable()
    layout_full_name = f"{args.object}-{args.layout}"

    print(f"Reading real profile assignments for '{layout_full_name}' from source org '{args.source_org}'...")
    assignments, error = get_source_layout_assignments(sf_exe, args.object, args.layout, args.source_org, args.sfdx_root)
    if error:
        print(f"ERROR: {error}")
        sys.exit(1)

    if not assignments:
        print("No profile assignments found for this layout in the source org. Nothing to recreate.")
        sys.exit(0)

    print(f"Found {len(assignments)} assignment(s):")
    for a in assignments:
        print(f"  {a['profile']}" + (f" ({a['record_type']})" if a["record_type"] else " (no record type)"))

    profile_map = None
    if args.profile_map:
        with open(args.profile_map, encoding="utf-8") as f:
            profile_map = json.load(f)
        print(f"\nUsing profile name mapping: {profile_map}")

    outdir = os.path.join(args.sfdx_root, "force-app", "main", "default", "profiles")
    written = generate_assignment_files(args.object, layout_full_name, assignments, outdir, profile_map)
    print(f"\nGenerated {len(written)} profile assignment file(s):")
    for path in written:
        print(f"  {path}")

    rel_paths = [os.path.relpath(p, args.sfdx_root) for p in written]
    deploy_cmd = [sf_exe, "project", "deploy", "start",
                 "--source-dir"] + rel_paths + ["--target-org", args.target_org]
    if args.dry_run:
        deploy_cmd.append("--dry-run")

    print("\nDeploy command:")
    print("  " + " ".join(deploy_cmd))

    print("\nRunning it now...")
    result = subprocess.run(deploy_cmd, cwd=args.sfdx_root, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=180)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        print("\nDeploy reported errors -- common cause: a profile name or record type "
              "DeveloperName doesn't match between source and target orgs. If profile "
              "names differ, use --profile-map.")
        sys.exit(1)
    else:
        print("\nSuccess.")


if __name__ == "__main__":
    main()
