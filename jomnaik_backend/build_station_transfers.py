"""Create explicit, short transfers for verified same-name rail interchanges."""

import csv
import io
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
import re
import unicodedata
import zipfile


DATA_DIRECTORY = Path(__file__).parent
RAIL_FILE = "gtfs-rail.zip"
KTM_FILE = "gtfs-ktmb.zip"
TRANSFER_FIELDS = ["from_stop_id", "to_stop_id", "transfer_type", "min_transfer_time"]


def normalized_station_name(name):
    value = unicodedata.normalize("NFKD", name).upper()
    value = re.sub(r"\b(REDONE|STATION|LRT|MRT|BRT|KTM|KOMUTER)\b", " ", value)
    return re.sub(r"[^A-Z0-9]", "", value)


def distance_meters(first, second):
    lat_1, lon_1, lat_2, lon_2 = map(radians, (*first, *second))
    delta_lat, delta_lon = lat_2 - lat_1, lon_2 - lon_1
    return 6_371_000 * 2 * asin(
        sqrt(
            sin(delta_lat / 2) ** 2
            + cos(lat_1) * cos(lat_2) * sin(delta_lon / 2) ** 2
        )
    )


def transfer_seconds(distance):
    if distance <= 30:
        return 60  # Same platform or cross-platform interchange.
    if distance <= 100:
        return 120
    return 240  # Same named station, connected by a short concourse walk.


def read_stops(filename):
    with zipfile.ZipFile(DATA_DIRECTORY / filename) as archive:
        return list(
            csv.DictReader(
                io.TextIOWrapper(archive.open("stops.txt"), encoding="utf-8-sig")
            )
        )


def write_transfers(filename, rows):
    path = DATA_DIRECTORY / filename
    temporary_path = path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(path) as archive:
        files = {
            name: archive.read(name)
            for name in archive.namelist()
            if name != "transfers.txt"
        }
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=TRANSFER_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    files["transfers.txt"] = output.getvalue().encode()
    with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    temporary_path.replace(path)


rail_stops = read_stops(RAIL_FILE)
ktm_stops = read_stops(KTM_FILE)
rail_rows = []
ktm_rows = []
seen = set()


def add_pair(first, second, first_rows, second_rows, minimum_seconds=None):
    key = (first["stop_id"], second["stop_id"])
    if key in seen:
        return
    seen.add(key)
    distance = distance_meters(
        (float(first["stop_lat"]), float(first["stop_lon"])),
        (float(second["stop_lat"]), float(second["stop_lon"])),
    )
    seconds = str(minimum_seconds or transfer_seconds(distance))
    first_rows.append({
        "from_stop_id": first["stop_id"],
        "to_stop_id": second["stop_id"],
        "transfer_type": "2",
        "min_transfer_time": seconds,
    })
    second_rows.append({
        "from_stop_id": second["stop_id"],
        "to_stop_id": first["stop_id"],
        "transfer_type": "2",
        "min_transfer_time": seconds,
    })


# Same-name stations in the Rapid Rail/BRT feed, with different route IDs.
for index, first in enumerate(rail_stops):
    first_name = normalized_station_name(first["stop_name"])
    for second in rail_stops[index + 1:]:
        if first.get("route_id") == second.get("route_id"):
            continue
        if first_name != normalized_station_name(second["stop_name"]):
            continue
        if distance_meters(
            (float(first["stop_lat"]), float(first["stop_lon"])),
            (float(second["stop_lat"]), float(second["stop_lon"])),
        ) <= 250:
            add_pair(first, second, rail_rows, rail_rows)

# KL Sentral and Muzium Negara are linked by a signed pedestrian connection.
# Keep the real concourse-walk allowance so OTP uses it only when it saves
# time compared with staying on or changing at another interchange.
stops_by_id = {stop["stop_id"]: stop for stop in rail_stops}
for from_stop_id, to_stop_id, seconds in (
    ("KJ15", "KG15", 300),
    ("MR1", "KG15", 420),
):
    add_pair(
        stops_by_id[from_stop_id],
        stops_by_id[to_stop_id],
        rail_rows,
        rail_rows,
        minimum_seconds=seconds,
    )

write_transfers(RAIL_FILE, rail_rows)
# GTFS transfer records may only reference stops in their own archive. KTM
# interchanges retain their shared OSM coordinates; its archive intentionally
# receives an empty transfer table rather than invalid cross-feed references.
write_transfers(KTM_FILE, ktm_rows)
print(
    f"Wrote {len(rail_rows)} rail/BRT transfers and "
    "kept KTM interchanges on their shared OSM station locations."
)
