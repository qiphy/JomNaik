"""Keep every locally bundled GTFS service calendar valid through 2030."""

import csv
import io
from pathlib import Path
import zipfile


DATA_DIRECTORY = Path(__file__).parent
GTFS_FILES = (
    "gtfs-rail.zip",
    "gtfs-ktmb.zip",
    "gtfs-bus.zip",
    "gtfs-mrtfeeder.zip",
)
END_DATE = "20301231"


for filename in GTFS_FILES:
    path = DATA_DIRECTORY / filename
    temporary_path = path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(path) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}

    if "calendar.txt" not in files:
        continue
    rows = list(
        csv.DictReader(
            io.TextIOWrapper(io.BytesIO(files["calendar.txt"]), encoding="utf-8-sig")
        )
    )
    for row in rows:
        row["end_date"] = END_DATE

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    files["calendar.txt"] = output.getvalue().encode()

    with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    temporary_path.replace(path)
    print(f"Extended {filename} service calendars through {END_DATE}.")
