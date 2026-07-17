"""Add all-day frequency rules to the retained Rapid Rail/BRT GTFS trips."""

import csv
import io
from pathlib import Path
import zipfile


GTFS_PATH = Path(__file__).with_name("gtfs-rail.zip")
TEMPORARY_PATH = GTFS_PATH.with_suffix(".zip.tmp")
WEEKDAY_SERVICES = {"MonFri", "MonThu", "Fri", "weekday"}


with zipfile.ZipFile(GTFS_PATH) as archive:
    files = {
        name: archive.read(name)
        for name in archive.namelist()
        if name not in {"frequencies.txt", "transfers.txt"}
    }

trips = list(
    csv.DictReader(
        io.TextIOWrapper(io.BytesIO(files["trips.txt"]), encoding="utf-8-sig")
    )
)
frequency_rows = [
    {
        "trip_id": trip["trip_id"],
        "start_time": "05:30:00",
        "end_time": "23:30:00",
        "headway_secs": "420"
        if trip["service_id"] in WEEKDAY_SERVICES
        else "600",
        "exact_times": "0",
    }
    for trip in trips
]
output = io.StringIO(newline="")
writer = csv.DictWriter(
    output,
    fieldnames=["trip_id", "start_time", "end_time", "headway_secs", "exact_times"],
)
writer.writeheader()
writer.writerows(frequency_rows)
files["frequencies.txt"] = output.getvalue().encode()

# BRT USJ 7 and LRT USJ 7 are a single interchange in practice but are
# separate GTFS stops. An explicit short transfer prevents OTP from walking
# from South Quay to the LRT instead of boarding the BRT for that connection.
transfer_output = io.StringIO(newline="")
transfer_writer = csv.DictWriter(
    transfer_output,
    fieldnames=["from_stop_id", "to_stop_id", "transfer_type", "min_transfer_time"],
)
transfer_writer.writeheader()
transfer_writer.writerows([
    {
        "from_stop_id": "BRT7",
        "to_stop_id": "KJ31",
        "transfer_type": "2",
        "min_transfer_time": "60",
    },
    {
        "from_stop_id": "KJ31",
        "to_stop_id": "BRT7",
        "transfer_type": "2",
        "min_transfer_time": "60",
    },
])
files["transfers.txt"] = transfer_output.getvalue().encode()

with zipfile.ZipFile(TEMPORARY_PATH, "w", zipfile.ZIP_DEFLATED) as archive:
    for name, content in files.items():
        archive.writestr(name, content)
TEMPORARY_PATH.replace(GTFS_PATH)

print(f"Added all-day frequencies for {len(frequency_rows)} rail and BRT trip patterns.")
