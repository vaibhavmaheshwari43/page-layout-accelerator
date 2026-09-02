#!/usr/bin/env python3
"""
Merges our existing, independently-validated registry (mapping_registry.json)
with the newly converted layout-mapping registry (from the mapping
accelerator's Excel output).

Priority rule: where OUR registry already has an entry for a field, KEEP
OURS -- those entries were validated against real deploy/describe data, not
just proposed. The new file's value here is covering fields we've never
seen before (other layouts, Address, Call2, etc.) -- those get added as-is,
still at whatever confidence the source data gives them.

IMPORTANT fix: matching used to be purely by (element_type, api_name),
which caused a real bug -- fields legitimately repurposed with a DIFFERENT
meaning per object (e.g. 'Name' = Account Name on Account, but Address
line 1 on Address_vod__c) would collide, and the object-specific entry got
silently dropped. Now also checks veeva_object: a new entry is only
skipped as "already covered" if the SAME (type, api_name, object)
combination already exists -- otherwise it's added as a genuinely
different, additional entry.
"""
import json
import sys

def main():
    existing_path, new_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(existing_path, encoding="utf-8") as f:
        existing = json.load(f)
    with open(new_path, encoding="utf-8") as f:
        new = json.load(f)

    existing_keys = {(e["element_type"], e["api_name"]) for e in existing["entries"]}
    existing_specific_keys = {
        (e["element_type"], e["api_name"], e["veeva_object"])
        for e in existing["entries"] if e.get("veeva_object")
    }

    added = 0
    added_as_object_specific = 0
    skipped_would_override_proven = 0
    # Our original 71 entries were ALL built exclusively in the Account/HCO
    # context -- so any new entry claiming veeva_object == 'Account' would
    # be competing with already-proven, hand-vetted data. Confirmed a real
    # case: a DIFFERENT Account layout ('Board') judged 'Name' as REFERENCE
    # (Retire), which would have silently overridden our correct, proven
    # HCO answer (Direct, High confidence) for the exact same field. Only
    # add object-specific entries for objects our original set never
    # covered at all -- that's genuinely new information, not a competing
    # opinion on something already verified.
    ORIGINAL_SET_IMPLICIT_OBJECT = "Account"

    merged_entries = list(existing["entries"])
    for e in new["entries"]:
        generic_key = (e["element_type"], e["api_name"])
        veeva_object = e.get("veeva_object")

        if veeva_object:
            specific_key = (e["element_type"], e["api_name"], veeva_object)
            if specific_key in existing_specific_keys:
                continue  # this exact object-specific entry already exists
            if generic_key in existing_keys and veeva_object == ORIGINAL_SET_IMPLICIT_OBJECT:
                # Would compete with already-proven data for the same
                # object -- trust the proven entry, skip this one.
                skipped_would_override_proven += 1
                continue
            if generic_key not in existing_keys:
                merged_entries.append(e)
                added += 1
            else:
                # Generic key exists, but this is a genuinely different
                # object (not Account) -- safe, valuable new coverage.
                merged_entries.append(e)
                added += 1
                added_as_object_specific += 1
        else:
            if generic_key not in existing_keys:
                merged_entries.append(e)
                added += 1

    print(f"Existing entries: {len(existing['entries'])}")
    print(f"New entries added (not previously covered): {added}")
    print(f"  Of which added as object-specific variants alongside an existing generic entry: {added_as_object_specific}")
    print(f"Skipped (would have overridden already-proven Account/HCO data): {skipped_would_override_proven}")
    print(f"Total merged: {len(merged_entries)}")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"entries": merged_entries}, f, indent=2)
    print(f"Written to {out_path}")

if __name__ == "__main__":
    main()

