"""Compare historical/rebuilt PE sections without executing either program.

Differences may arise from source changes, paths, compiler/linker inputs or
metadata. Section identity is evidence, not a replacement for provenance or
runtime acceptance. This tool never updates host/source correspondence flags.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct


def pe_sections(data: bytes) -> dict:
    if len(data) < 64 or data[:2] != b"MZ":
        raise ValueError("Not a DOS/PE image")
    offset = struct.unpack_from("<I", data, 60)[0]
    if offset + 24 > len(data) or data[offset:offset + 4] != b"PE\0\0":
        raise ValueError("Invalid PE header")
    machine, count, timestamp, _, _, optional_size, _ = struct.unpack_from("<HHIIIHH", data, offset + 4)
    table = offset + 24 + optional_size
    if count > 96 or table + count * 40 > len(data):
        raise ValueError("Invalid PE section table")
    result = {"machine": machine, "coff_timestamp": timestamp, "sections": {}}
    for index in range(count):
        row = table + index * 40
        name = data[row:row + 8].rstrip(b"\0").decode("ascii", errors="strict")
        size, address = struct.unpack_from("<II", data, row + 16)
        if name in result["sections"] or address + size > len(data):
            raise ValueError("Invalid PE section bounds/alias")
        result["sections"][name] = {"bytes": size, "sha256": hashlib.sha256(data[address:address + size]).hexdigest()}
    return result


def compare(first: Path, second: Path) -> dict:
    left, right = first.read_bytes(), second.read_bytes()
    before, after = pe_sections(left), pe_sections(right)
    names = sorted(set(before["sections"]) | set(after["sections"]))
    return {"schema": 1, "historical_sha256": hashlib.sha256(left).hexdigest(),
        "rebuilt_sha256": hashlib.sha256(right).hexdigest(), "full_image_equal": left == right,
        "section_comparison": {name: {"historical": before["sections"].get(name),
            "rebuilt": after["sections"].get(name),
            "equal": before["sections"].get(name) == after["sections"].get(name)} for name in names},
        "historical_coff_timestamp": before["coff_timestamp"],
        "rebuilt_coff_timestamp": after["coff_timestamp"],
        "historical_binary_source_correspondence_verified": False,
        "note": "Section hashes alone cannot establish source correspondence or runtime compatibility."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical", type=Path, required=True)
    parser.add_argument("--rebuilt", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.report.exists():
        raise FileExistsError("Use a new comparison report")
    result = compare(args.historical, args.rebuilt)
    args.report.write_text(json.dumps(result, indent=2) + "\n", "utf-8")
    print(json.dumps({"full_image_equal": result["full_image_equal"],
        "equal_sections": sum(x["equal"] for x in result["section_comparison"].values()),
        "historical_binary_source_correspondence_verified": False}))


if __name__ == "__main__":
    main()
