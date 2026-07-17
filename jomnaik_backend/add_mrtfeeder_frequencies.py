"""Add frequency rules derived from the MRT feeder's scheduled departures.

The feed contains scheduled trips but no frequencies.txt. OTP therefore treats
every feeder trip as an isolated departure, which creates large transfer waits.
This script derives a conservative headway per route/service/direction from the
first-stop departures and attaches one frequency pattern to a representative
trip in each group.
"""

import csv
import io
import statistics
import zipfile
from pathlib import Path


BASE = Path(__file__).parent
SOURCE = BASE / "gtfs-mrtfeeder.zip"
BACKUP = BASE / "gtfs-mrtfeeder.before-frequency.zip"
TEMP = BASE / "gtfs-mrtfeeder.zip.tmp"


def seconds(value: str) -> int:
    hours, minutes, secs = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + secs


def clock(value: int) -> str:
    value %= 24 * 3600
    return f"{value // 3600:02d}:{(value % 3600) // 60:02d}:{value % 60:02d}"


with zipfile.ZipFile(SOURCE) as archive:
    files = {name: archive.read(name) for name in archive.namelist()}

trips = list(csv.DictReader(io.TextIOWrapper(io.BytesIO(files["trips.txt"]), encoding="utf-8-sig")))
first_departures = {}
for row in csv.DictReader(io.TextIOWrapper(io.BytesIO(files["stop_times.txt"]), encoding="utf-8-sig")):
    if row.get("stop_sequence") == "1":
        first_departures[row["trip_id"]] = row["departure_time"]

groups = {}
for trip in trips:
    departure = first_departures.get(trip["trip_id"])
    if not departure:
        continue
    key = (trip["route_id"], trip["service_id"], trip.get("direction_id", ""))
    groups.setdefault(key, []).append((seconds(departure), trip["trip_id"]))

frequency_rows = []
for departures in groups.values():
    departures.sort()
    if len(departures) < 4:
        continue
    gaps = [right[0] - left[0] for left, right in zip(departures, departures[1:])]
    headway = max(600, min(3600, int(statistics.median(gaps))))
    frequency_rows.append({
        "trip_id": departures[0][1],
        "start_time": clock(departures[0][0]),
        "end_time": clock(departures[-1][0]),
        "headway_secs": str(headway),
        "exact_times": "0",
    })

output = io.StringIO(newline="")
writer = csv.DictWriter(output, fieldnames=["trip_id", "start_time", "end_time", "headway_secs", "exact_times"])
writer.writeheader()
writer.writerows(frequency_rows)
files["frequencies.txt"] = output.getvalue().encode()

if not BACKUP.exists():
    BACKUP.write_bytes(SOURCE.read_bytes())
with zipfile.ZipFile(TEMP, "w", zipfile.ZIP_DEFLATED) as archive:
    for name, content in files.items():
        archive.writestr(name, content)
TEMP.replace(SOURCE)
print(f"Added {len(frequency_rows)} data-derived MRT feeder frequency patterns.")
