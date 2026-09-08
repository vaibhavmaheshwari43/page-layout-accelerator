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

SECOND IMPORTANT FIX: the previous version treated "already exists" as the
end of the story for object-specific entries, with no way to detect that
the mapping team's own file had been genuinely UPDATED since the last
conversion. Confirmed a real case: Address_vod__c's 'Name' field was
REFERENCE/Retire in an earlier file, then corrected to MAPPED/Map -> 
Address.Street in a newer one -- a legitimate correction that was being
silently discarded. Now: for NON-ACCOUNT object-specific entries (where we
have no independently-proven data of our own, only what the mapping team's
file says), if the new file's answer for the SAME field genuinely differs
from what's on record, it's treated as an update and replaces the old
entry. Account entries are NOT subject to this -- that trust-hierarchy
protection stays absolute and unconditional, since our Account data is
independently proven via real deployment testing, not just the mapping
team's latest opinion.
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
    # Index by specific key for quick lookup/replacement during the update check.
    entry_by_specific_key = {
        (e["element_type"], e["api_name"], e["veeva_object"]): e
        for e in existing["entries"] if e.get("veeva_object")
    }

    added = 0
    added_as_object_specific = 0
    updated = 0
    skipped_would_override_proven = 0
    skipped_unchanged = 0
    ORIGINAL_SET_IMPLICIT_OBJECT = "Account"

    merged_entries = list(existing["entries"])
    for e in new["entries"]:
        generic_key = (e["element_type"], e["api_name"])
        veeva_object = e.get("veeva_object")

        if veeva_object:
            specific_key = (e["element_type"], e["api_name"], veeva_object)
            if specific_key in existing_specific_keys:
                if veeva_object == ORIGINAL_SET_IMPLICIT_OBJECT:
                    # Account: NEVER update, even if the new file disagrees --
                    # this protection stays unconditional, since our Account
                    # data is independently proven, not just the mapping
                    # team's latest opinion.
                    continue
                old_entry = entry_by_specific_key[specific_key]
                content_differs = (old_entry["classification"]["canonical"] != e["classification"]["canonical"]
                                   or old_entry["target"] != e["target"])
                if content_differs:
                    merged_entries.remove(old_entry)
                    merged_entries.append(e)
                    updated += 1
                else:
                    skipped_unchanged += 1
                continue
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
    print(f"Updated (mapping team corrected a non-Account entry since the last conversion): {updated}")
    print(f"Skipped, unchanged (identical to what's already on file): {skipped_unchanged}")
    print(f"Skipped (would have overridden already-proven Account/HCO data): {skipped_would_override_proven}")
    print(f"Total merged: {len(merged_entries)}")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"entries": merged_entries}, f, indent=2)
    print(f"Written to {out_path}")

if __name__ == "__main__":
    main()

