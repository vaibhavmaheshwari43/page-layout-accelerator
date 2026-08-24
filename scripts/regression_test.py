#!/usr/bin/env python3
"""
Veeva -> LSC Page Layout Accelerator — Regression Test

Runs the ENTIRE pipeline (extract -> classify -> generate -> validate)
against the one layout we have a verified, hand-built answer for (HCO), and
fails loudly if anything no longer matches exactly.

This exists so that every future code change (bug fixes, new features,
registry edits) gets checked automatically against a known-good baseline,
instead of someone manually re-running 3 commands and eyeballing the
output each time.

Run this after ANY change to extractor.py, rules_engine.py, generator.py,
or mapping_registry.json:

    python regression_test.py

Exits with code 0 if everything still matches, 1 if anything regressed.
"""

import json
import os
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# These paths assume the standard project layout. Adjust if yours differs.
DEFAULTS = {
    "veeva_xml": os.path.join(SCRIPT_DIR, "..", "reference",
                              "Veeva_Source_Account-SP_Admin_Layout_HCO_layout-meta.xml"),
    "registry": os.path.join(SCRIPT_DIR, "..", "registry", "mapping_registry.json"),
    "ref_layout": os.path.join(SCRIPT_DIR, "..", "reference",
                                "LSC_Target_Account_SP_Admin_Layout_HCO_layout-meta.xml"),
    "ref_flexipage": os.path.join(SCRIPT_DIR, "..", "reference",
                                   "Account_HCO_Admin_Record_Page_flexipage-meta.xml"),
}

EXPECTED = {
    "total_elements": 66,
    "registry_matched": 65,
    "registry_unmatched": 0,
    "auto_generate": 49,
    "flag_decision_needed": 8,
    "flag_rebuild": 3,
    "flag_retire": 3,
    "flag_manual_review": 2,
    "fields_placed": 41,
}


def run_step(description, cmd):
    print(f"\n--- {description} ---")
    print("  $", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        print(f"FAILED: {description}")
        sys.exit(1)
    return result.stdout


def find_path(key, override):
    path = override or DEFAULTS[key]
    if not os.path.isfile(path):
        print(f"ERROR: expected file not found for '{key}': {path}")
        print("Pass the correct path explicitly with the matching --* argument, "
              "or check your project layout matches the standard structure.")
        sys.exit(1)
    return path


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Regression test the pipeline against the known-good HCO baseline.")
    ap.add_argument("--veeva-xml", default=None)
    ap.add_argument("--registry", default=None)
    ap.add_argument("--ref-layout", default=None)
    ap.add_argument("--ref-flexipage", default=None)
    args = ap.parse_args()

    veeva_xml = find_path("veeva_xml", args.veeva_xml)
    registry = find_path("registry", args.registry)
    ref_layout = find_path("ref_layout", args.ref_layout)
    ref_flexipage = find_path("ref_flexipage", args.ref_flexipage)

    with tempfile.TemporaryDirectory() as tmpdir:
        classified_path = os.path.join(tmpdir, "classified.json")
        run_step(
            "1/3 Extraction + Classification",
            ["python3", os.path.join(SCRIPT_DIR, "rules_engine.py"),
             "--object", "Account", "--layout", "SP_Admin_Layout_HCO",
             "--xml", veeva_xml, "--registry", registry, "--out", classified_path],
        )

        with open(classified_path, encoding="utf-8") as f:
            classified_data = json.load(f)

        checks = []
        checks.append(("total_elements", classified_data["total_elements"], EXPECTED["total_elements"]))
        checks.append(("registry_matched", classified_data["registry_matched"], EXPECTED["registry_matched"]))
        checks.append(("registry_unmatched", classified_data["registry_unmatched"], EXPECTED["registry_unmatched"]))
        for action in ("auto_generate", "flag_decision_needed", "flag_rebuild", "flag_retire", "flag_manual_review"):
            checks.append((action, classified_data["action_summary"].get(action, 0), EXPECTED[action]))

        if classified_data["registry_load_conflicts"]["status"] != "OK":
            print("FAILED: registry has internal conflicts:", classified_data["registry_load_conflicts"])
            sys.exit(1)
        if classified_data["duplicate_consistency_check"]["status"] != "OK":
            print("FAILED: duplicate consistency check failed:", classified_data["duplicate_consistency_check"])
            sys.exit(1)

        run_step(
            "2/3 Generation",
            ["python3", os.path.join(SCRIPT_DIR, "generator.py"),
             "--object", "Account", "--layout", "SP_Admin_Layout_HCO",
             "--classified", classified_path, "--outdir", tmpdir],
        )

        gen_layout = os.path.join(tmpdir, "Account-SP_Admin_Layout_HCO_Generated.layout-meta.xml")
        gen_flexipage = os.path.join(tmpdir, "Account_SP_Admin_Layout_HCO_Generated.flexipage-meta.xml")
        build_report = os.path.join(tmpdir, "SP_Admin_Layout_HCO_build_report.json")

        validator_output = run_step(
            "3/3 Validation (self-consistency + reference comparison)",
            ["python3", os.path.join(SCRIPT_DIR, "validator.py"),
             "--generated-layout", gen_layout, "--generated-flexipage", gen_flexipage,
             "--build-report", build_report,
             "--reference-layout", ref_layout, "--reference-flexipage", ref_flexipage],
        )

        checks.append(("validator_overall", "PASS" in validator_output.split("OVERALL:")[-1], True))

        print("\n" + "=" * 60)
        print("REGRESSION TEST RESULTS")
        print("=" * 60)
        all_pass = True
        for name, actual, expected in checks:
            status = "PASS" if actual == expected else "FAIL"
            if status == "FAIL":
                all_pass = False
            print(f"  [{status}] {name}: expected {expected}, got {actual}")

        print("=" * 60)
        if all_pass:
            print("ALL CHECKS PASSED \u2014 no regression from the known-good HCO baseline.")
            sys.exit(0)
        else:
            print("REGRESSION DETECTED \u2014 something changed vs. the known-good baseline. "
                  "Review the FAIL rows above before trusting this code change.")
            sys.exit(1)


if __name__ == "__main__":
    main()
