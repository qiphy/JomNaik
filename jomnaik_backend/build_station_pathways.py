"""Generate GTFS pedestrian pathways for every permitted transfer pair.

The routing engine uses pathways as navigable in-station links. The official feeds provide
transfer relationships but not their physical pedestrian edges, so this turns
each valid transfer into a directed walkway without naming any interchange.
"""

import csv
import io
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
import zipfile


DATA_DIRECTORY = Path(__file__).parent
FEEDS = ("gtfs-rail.zip", "gtfs-ktmb.zip")
PATHWAY_FIELDS = [
    "pathway_id", "from_stop_id", "to_stop_id", "pathway_mode",
    "is_bidirectional", "length", "traversal_time",
]


def distance_meters(first, second):
    lat_1, lon_1, lat_2, lon_2 = map(radians, (*first, *second))
    delta_lat, delta_lon = lat_2 - lat_1, lon_2 - lon_1
    return 6_371_000 * 2 * asin(sqrt(
        sin(delta_lat / 2) ** 2
        + cos(lat_1) * cos(lat_2) * sin(delta_lon / 2) ** 2
    ))


def rows_from_zip(archive, name):
    if name not in archive.namelist():
        return []
    return list(csv.DictReader(
        io.TextIOWrapper(archive.open(name), encoding="utf-8-sig")
    ))


def build_pathways(filename):
    path = DATA_DIRECTORY / filename
    temporary = path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(path) as archive:
        stops = {
            row["stop_id"]: row
            for row in rows_from_zip(archive, "stops.txt")
        }
        transfers = rows_from_zip(archive, "transfers.txt")
        files = {
            name: archive.read(name)
            for name in archive.namelist()
            if name != "pathways.txt"
        }

    pathways = []
    seen = set()
    for transfer in transfers:
        # Transfer type 3 explicitly prohibits a connection.
        if str(transfer.get("transfer_type") or "").strip() == "3":
            continue
        from_stop = stops.get(transfer.get("from_stop_id"))
        to_stop = stops.get(transfer.get("to_stop_id"))
        if from_stop is None or to_stop is None:
            continue
        key = (from_stop["stop_id"], to_stop["stop_id"])
        if key in seen:
            continue
        seen.add(key)
        try:
            length = distance_meters(
                (float(from_stop["stop_lat"]), float(from_stop["stop_lon"])),
                (float(to_stop["stop_lat"]), float(to_stop["stop_lon"])),
            )
            configured_time = int(float(transfer.get("min_transfer_time") or 0))
        except (KeyError, TypeError, ValueError):
            continue
        # Use transfer data when supplied; otherwise calculate a conservative
        # universal pedestrian allowance at 1.2 m/s, with a 30-second floor.
        traversal_time = configured_time or max(30, round(length / 1.2))
        pathways.append({
            "pathway_id": f"transfer_{from_stop['stop_id']}_{to_stop['stop_id']}",
            "from_stop_id": from_stop["stop_id"],
            "to_stop_id": to_stop["stop_id"],
            "pathway_mode": "1",
            # Each transfer record retains its own direction. The generated
            # transfer file provides the matching reverse record where valid.
            "is_bidirectional": "0",
            "length": f"{length:.1f}",
            "traversal_time": str(traversal_time),
        })

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=PATHWAY_FIELDS)
    writer.writeheader()
    writer.writerows(pathways)
    files["pathways.txt"] = output.getvalue().encode()
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    temporary.replace(path)
    return len(pathways)


counts = {filename: build_pathways(filename) for filename in FEEDS}
print("Generated GTFS transfer pathways:", counts)
