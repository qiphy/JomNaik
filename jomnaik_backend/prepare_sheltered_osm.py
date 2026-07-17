#!/usr/bin/env python3
"""Create an OTP-specific OSM extract that favours sheltered pedestrian ways.

OTP 2.4 has no configuration hook for assigning a walk-safety cost to
``covered=*``.  Its built-in mapper does, however, assign a low walk-safety
factor to ``highway=path``.  This script makes a *derived* PBF for OTP only:
covered/indoor pedestrian ways are represented as paths, while the source PBF
and the original highway value remain untouched.
"""

from __future__ import annotations

import argparse
import copy
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


PEDESTRIAN_HIGHWAYS = {"footway", "path", "pedestrian", "corridor", "steps"}
COVERED_VALUES = {"yes", "arcade"}


def is_sheltered(tags: dict[str, str]) -> bool:
    return (
        tags.get("covered") in COVERED_VALUES
        or tags.get("indoor") == "yes"
        or tags.get("highway") == "corridor"
        or tags.get("tunnel") == "building_passage"
    )


def sheltered_way_change(source_pbf: Path, change_file: Path) -> int:
    """Write an OSM change file that marks walkable sheltered ways as paths."""
    with tempfile.TemporaryDirectory() as directory:
        selected_ways = Path(directory) / "sheltered-ways.osm"
        subprocess.run(
            [
                "osmium",
                "tags-filter",
                "-R",
                "-f",
                "osm",
                str(source_pbf),
                "w/covered=yes",
                "w/covered=arcade",
                "w/indoor=yes",
                "w/highway=corridor",
                "w/tunnel=building_passage",
                "-o",
                str(selected_ways),
                "-O",
            ],
            check=True,
        )
        selected_root = ET.parse(selected_ways).getroot()

    change_root = ET.Element("osmChange", {"version": "0.6", "generator": "JomNaik"})
    modify = ET.SubElement(change_root, "modify")
    count = 0
    for way in selected_root.findall("way"):
        tags = {tag.attrib["k"]: tag.attrib["v"] for tag in way.findall("tag")}
        highway = tags.get("highway")
        if highway not in PEDESTRIAN_HIGHWAYS or not is_sheltered(tags):
            continue

        updated_way = ET.SubElement(modify, "way", dict(way.attrib))
        for node_ref in way.findall("nd"):
            updated_way.append(copy.deepcopy(node_ref))
        for tag in way.findall("tag"):
            if tag.attrib["k"] != "highway":
                updated_way.append(copy.deepcopy(tag))
        ET.SubElement(updated_way, "tag", {"k": "highway", "v": "path"})
        ET.SubElement(updated_way, "tag", {"k": "jomnaik:original_highway", "v": highway})
        ET.SubElement(updated_way, "tag", {"k": "jomnaik:sheltered", "v": "yes"})
        count += 1

    ET.indent(change_root, space="  ")
    ET.ElementTree(change_root).write(change_file, encoding="utf-8", xml_declaration=True)
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="Original OSM PBF")
    parser.add_argument("output", type=Path, help="Derived PBF for OTP")
    parser.add_argument(
        "--change-file",
        type=Path,
        default=Path("sheltered_walkway_routing.osc"),
        help="Auditable OSM change file to produce",
    )
    args = parser.parse_args()

    count = sheltered_way_change(args.source, args.change_file)
    subprocess.run(
        [
            "osmium",
            "apply-changes",
            str(args.source),
            str(args.change_file),
            "-o",
            str(args.output),
            "-O",
        ],
        check=True,
    )
    print(f"Prepared {args.output} with {count} sheltered pedestrian ways.")


if __name__ == "__main__":
    main()
