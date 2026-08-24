#!/usr/bin/env python3
"""
Quick structural comparison: your hand-built Page Layout vs. the generated one.
Ignores formatting/ordering noise — only checks what actually matters:
same field, same section, same column, same behavior.

Usage:
    python3 compare_layouts.py real_file.xml generated_file.xml
"""
import sys
import xml.etree.ElementTree as ET

NS = {"sf": "http://soap.sforce.com/2006/04/metadata"}


def get_fields(path):
    root = ET.parse(path).getroot()
    result = {}
    for sec in root.findall("sf:layoutSections", NS):
        label = sec.find("sf:label", NS)
        label = label.text if label is not None else "(no label)"
        for col_idx, col in enumerate(sec.findall("sf:layoutColumns", NS), start=1):
            for item in col.findall("sf:layoutItems", NS):
                f = item.find("sf:field", NS)
                b = item.find("sf:behavior", NS)
                if f is not None:
                    result[(label, col_idx, f.text)] = b.text if b is not None else None
    return result


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 compare_layouts.py <real_file.xml> <generated_file.xml>")
        sys.exit(1)

    real = get_fields(sys.argv[1])
    gen = get_fields(sys.argv[2])

    only_real = sorted(set(real) - set(gen))
    only_gen = sorted(set(gen) - set(real))
    common = set(real) & set(gen)
    mismatches = [(k, real[k], gen[k]) for k in common if real[k] != gen[k]]

    print(f"Fields in hand-built file: {len(real)}")
    print(f"Fields in generated file: {len(gen)}")
    print()

    print(f"In hand-built but NOT generated ({len(only_real)}):")
    for k in only_real:
        print("  ", k)
    print()

    print(f"In generated but NOT hand-built ({len(only_gen)}) — should be 0:")
    for k in only_gen:
        print("  ", k)
    print()

    print(f"Behavior mismatches ({len(mismatches)}) — should be 0:")
    for m in mismatches:
        print("  ", m)
    print()

    exact = len(common) - len(mismatches)
    print(f"Exact matches: {exact} / {len(real)}")


if __name__ == "__main__":
    main()
