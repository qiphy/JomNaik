from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import os
import logging
import zipfile
import io
import csv
import copy
from collections import defaultdict
from html.parser import HTMLParser
from datetime import datetime, timedelta
from functools import lru_cache
import json
import re
import unicodedata
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from time import monotonic, sleep
from zoneinfo import ZoneInfo

from google.transit import gtfs_realtime_pb2
from motis_adapter import plan as motis_plan, walk as motis_walk

app = FastAPI(title="JomNaik Routing Middleware")
logger = logging.getLogger("jomnaik")
APP_TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Kuala_Lumpur"))


def _local_now():
    """Return Malaysia local time regardless of the container's OS timezone."""
    return datetime.now(APP_TIMEZONE)


DATA_DIRECTORY = Path(__file__).parent
DEPARTURE_SCHEDULES_FILE = DATA_DIRECTORY / "departure_schedules.json"
COVERED_WALKWAYS_FILE = DATA_DIRECTORY / "covered_walkways.geojsonseq"
STATION_COORDINATE_AUDIT_FILE = DATA_DIRECTORY / "station_coordinate_audit.json"
KTM_FARE_FILE = DATA_DIRECTORY / "ktm_komuter_fares.json"
REALTIME_FEEDS = {
    "gtfs-bus.zip": "https://api.data.gov.my/gtfs-realtime/vehicle-position/prasarana?category=rapid-bus-kl",
    "gtfs-mrtfeeder.zip": "https://api.data.gov.my/gtfs-realtime/vehicle-position/prasarana?category=rapid-bus-mrtfeeder",
    "gtfs-rail.zip": "https://api.data.gov.my/gtfs-realtime/vehicle-position/prasarana?category=rapid-rail-kl",
}
_realtime_cache = {"loaded_at": 0.0, "vehicles": []}


def _configured_value(name):
    """Read an environment setting, falling back to this service's .env file."""
    value = os.getenv(name)
    if value:
        return value
    try:
        for line in (DATA_DIRECTORY / ".env").read_text(encoding="utf-8").splitlines():
            key, separator, configured_value = line.partition("=")
            if separator and key.strip() == name:
                return configured_value.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in (
        _configured_value("CORS_ALLOW_ORIGINS")
        or "http://localhost:3000,http://127.0.0.1:3000,http://152.42.181.141,https://152.42.181.141"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


TOMTOM_API_KEY = _configured_value("TOMTOM_API_KEY")
TOMTOM_FLOW_URL = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
SUPABASE_URL = _configured_value("SUPABASE_URL").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = _configured_value("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_STATION_COORDINATES_TABLE = _configured_value(
    "SUPABASE_STATION_COORDINATES_TABLE"
) or "station_coordinates"
SUPABASE_PRESENCE_TABLE = _configured_value("SUPABASE_PRESENCE_TABLE") or "anonymous_station_presence"
SUPABASE_INCIDENT_REPORTS_TABLE = _configured_value(
    "SUPABASE_INCIDENT_REPORTS_TABLE"
) or "anonymous_incident_reports"
_traffic_cache = {}
TRAFFIC_CACHE_SECONDS = 60
MAX_TOMTOM_LOOKUPS_PER_REQUEST = 6
STATION_PRESENCE_WINDOW_MINUTES = 15
INCIDENT_REPORT_WINDOW_MINUTES = 20
EHAILING_RATE_PER_KM = 1.50
MAX_PUBLIC_TRANSPORT_TRANSFER_WAIT_SECONDS = 10 * 60
LAST_MILE_EHAILING_THRESHOLD_METERS = 1000
STATION_AREA_RADIUS_METERS = 110
STATION_WALKWAY_RADIUS_METERS = 180
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
RAPIDKL_FARE_TABLE_URL = "https://mrt.com.my/fare/fares-master10.htm"
OSRM_DRIVING_URL = os.getenv(
    "OSRM_DRIVING_URL", "https://router.project-osrm.org/route/v1/driving"
)
_geocode_cache = {}
_last_geocode_request_at = 0.0
_covered_walkway_grid = None
_COVERED_WALKWAY_GRID_SIZE = 0.001
_SHELTER_MATCH_DISTANCE_METERS = 18


class RouteRequest(BaseModel):
    from_lat: float
    from_lon: float
    to_lat: float
    to_lon: float
    departure_date: str | None = None
    departure_time: str | None = None


@app.get("/api/health")
def health_check():
    return {"status": "ok", "routingEngine": "motis"}


@app.get("/api/places/search")
def search_places(query: str = Query(min_length=2, max_length=120)):
    """Search for a place after an explicit user submission, not autocomplete."""
    global _last_geocode_request_at
    normalized_query = " ".join(query.split())
    cache_key = normalized_query.casefold()
    cached = _geocode_cache.get(cache_key)
    if cached is not None:
        return {"results": cached}

    wait_seconds = 1 - (monotonic() - _last_geocode_request_at)
    if wait_seconds > 0:
        sleep(wait_seconds)

    try:
        response = requests.get(
            NOMINATIM_SEARCH_URL,
            params={
                "q": normalized_query,
                "format": "jsonv2",
                "addressdetails": 1,
                "limit": 5,
                "countrycodes": "my",
            },
            headers={"User-Agent": "JomNaik/1.0 (location search)"},
            timeout=8,
        )
        _last_geocode_request_at = monotonic()
        response.raise_for_status()
        results = []
        for place in response.json():
            display_name = place.get("display_name", "")
            name = place.get("name") or display_name.split(",", 1)[0]
            try:
                results.append({
                    "name": name,
                    "address": display_name,
                    "lat": float(place["lat"]),
                    "lon": float(place["lon"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
        _geocode_cache[cache_key] = results
        return {"results": results}
    except requests.exceptions.RequestException:
        raise HTTPException(status_code=503, detail="Location search is temporarily unavailable.")


@app.get("/api/places/reverse")
def reverse_place(latitude: float = Query(ge=-90, le=90), longitude: float = Query(ge=-180, le=180)):
    """Resolve a long-pressed map coordinate to a passenger-facing address."""
    global _last_geocode_request_at
    cache_key = f"reverse:{latitude:.5f},{longitude:.5f}"
    cached = _geocode_cache.get(cache_key)
    if cached is not None:
        return cached

    wait_seconds = 1 - (monotonic() - _last_geocode_request_at)
    if wait_seconds > 0:
        sleep(wait_seconds)
    try:
        response = requests.get(
            NOMINATIM_REVERSE_URL,
            params={
                "lat": latitude,
                "lon": longitude,
                "format": "jsonv2",
                "addressdetails": 1,
                "zoom": 18,
            },
            headers={"User-Agent": "JomNaik/1.0 (map location lookup)"},
            timeout=8,
        )
        _last_geocode_request_at = monotonic()
        response.raise_for_status()
        place = response.json()
        address = place.get("address") or {}
        name = (
            place.get("name")
            or address.get("amenity")
            or address.get("building")
            or address.get("road")
            or "Dropped pin"
        )
        result = {
            "name": name,
            "address": place.get("display_name") or "Selected map location",
            "lat": float(place.get("lat", latitude)),
            "lon": float(place.get("lon", longitude)),
        }
        _geocode_cache[cache_key] = result
        return result
    except (requests.exceptions.RequestException, TypeError, ValueError):
        return {
            "name": "Dropped pin",
            "address": f"{latitude:.5f}, {longitude:.5f}",
            "lat": latitude,
            "lon": longitude,
        }


@lru_cache(maxsize=1)
def _scheduled_departures_by_stop():
    try:
        with DEPARTURE_SCHEDULES_FILE.open(encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=1)
def _scheduled_rail_terminals():
    """Map scheduled rail/BRT calls to their GTFS destination terminals."""
    terminals = {}
    for filename in ("gtfs-rail.zip", "gtfs-ktmb.zip"):
        zip_path = DATA_DIRECTORY / filename
        if not zip_path.exists():
            continue
        try:
            with zipfile.ZipFile(zip_path, "r") as archive:
                def read_csv(name):
                    with archive.open(name) as file:
                        return list(csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig")))

                routes = {row["route_id"]: row for row in read_csv("routes.txt")}
                trips = {row["trip_id"]: row for row in read_csv("trips.txt")}
                
                if "calendar.txt" in archive.namelist():
                    service_days = {
                        row["service_id"]: "".join(
                            row[day] for day in (
                                "monday", "tuesday", "wednesday", "thursday",
                                "friday", "saturday", "sunday",
                            )
                        )
                        for row in read_csv("calendar.txt")
                    }
                else:
                    service_days = {}

                for stop_time in read_csv("stop_times.txt"):
                    trip = trips.get(stop_time["trip_id"])
                    if trip is None:
                        continue
                    route = routes.get(trip["route_id"], {})
                    route_name = route.get("route_short_name") or route.get("route_long_name")
                    active_days = service_days.get(trip.get("service_id"))
                    terminal = _terminal_station(trip)
                    if not route_name or not active_days or not terminal:
                        continue
                    key = (stop_time["stop_id"], route_name, stop_time["arrival_time"], active_days)
                    terminals.setdefault(key, set()).add(terminal)
        except (zipfile.BadZipFile, KeyError, OSError):
            continue
    return terminals


@lru_cache(maxsize=1)
def _frequency_calls_by_stop():
    """Index GTFS frequency-based rail/BRT calls with their terminal signs."""
    calls = defaultdict(list)
    for filename in ("gtfs-rail.zip", "gtfs-ktmb.zip"):
        zip_path = DATA_DIRECTORY / filename
        if not zip_path.exists():
            continue
        try:
            with zipfile.ZipFile(zip_path, "r") as archive:
                if "frequencies.txt" not in archive.namelist():
                    continue

                def read_csv(name):
                    with archive.open(name) as file:
                        return list(csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig")))

                routes = {row["route_id"]: row for row in read_csv("routes.txt")}
                trips = {row["trip_id"]: row for row in read_csv("trips.txt")}
                
                if "calendar.txt" in archive.namelist():
                    service_days = {
                        row["service_id"]: "".join(
                            row[day] for day in (
                                "monday", "tuesday", "wednesday", "thursday",
                                "friday", "saturday", "sunday",
                            )
                        )
                        for row in read_csv("calendar.txt")
                    }
                else:
                    service_days = {}

                stop_times = defaultdict(list)
                for row in read_csv("stop_times.txt"):
                    stop_times[row["trip_id"]].append(row)

                for frequency in read_csv("frequencies.txt"):
                    trip = trips.get(frequency.get("trip_id"))
                    if trip is None:
                        continue
                    route = routes.get(trip.get("route_id"), {})
                    route_name = route.get("route_short_name") or route.get("route_long_name")
                    terminal = _terminal_station(trip)
                    active_days = service_days.get(trip.get("service_id"))
                    if not route_name or not terminal or not active_days:
                        continue
                    try:
                        start_seconds = _clock_seconds(frequency["start_time"])
                        end_seconds = _clock_seconds(frequency["end_time"])
                        headway_seconds = int(frequency["headway_secs"])
                        trip_calls = sorted(
                            stop_times[trip["trip_id"]],
                            key=lambda row: int(row.get("stop_sequence") or 0),
                        )
                        first_departure = _clock_seconds(trip_calls[0]["departure_time"])
                    except (KeyError, TypeError, ValueError, IndexError):
                        continue
                    for stop_time in trip_calls:
                        try:
                            offset_seconds = _clock_seconds(stop_time["departure_time"]) - first_departure
                        except (KeyError, TypeError, ValueError):
                            continue
                        calls[stop_time["stop_id"]].append({
                            "route": route_name,
                            "terminal": terminal,
                            "activeDays": active_days,
                            "first": start_seconds + offset_seconds,
                            "last": end_seconds + offset_seconds,
                            "headway": headway_seconds,
                        })
        except (zipfile.BadZipFile, KeyError, OSError):
            continue
    return dict(calls)


def _clock_seconds(value):
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _frequency_departures(stop_id, now):
    """Return the next calls from GTFS frequencies, retaining terminals."""
    departures = []
    for call in _frequency_calls_by_stop().get(stop_id, []):
        for day_offset in range(2):
            day = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)
            if day.weekday() >= len(call["activeDays"]) or call["activeDays"][day.weekday()] != "1":
                continue
            first = day + timedelta(seconds=call["first"])
            last = day + timedelta(seconds=call["last"])
            if last < now:
                continue
            elapsed = max(0, int((now - first).total_seconds()))
            step = max(0, (elapsed + call["headway"] - 1) // call["headway"])
            departure = first + timedelta(seconds=step * call["headway"])
            if departure <= last:
                departures.append({
                    "route": call["route"],
                    "direction": call["terminal"],
                    "is_bus": False,
                    "time": departure.strftime("%I:%M %p").lstrip("0"),
                    "timestamp": int(departure.timestamp() * 1000),
                    "is_estimated": False,
                })
    return departures


def _next_departure_datetime(time_value, active_days, now):
    try:
        hours, minutes, seconds = (int(value) for value in time_value.split(":"))
    except ValueError:
        return None

    for day_offset in range(8):
        scheduled = now.replace(hour=0, minute=0, second=0, microsecond=0)
        scheduled += timedelta(days=day_offset, hours=hours, minutes=minutes, seconds=seconds)
        if scheduled.weekday() >= len(active_days) or active_days[scheduled.weekday()] != "1" or scheduled < now:
            continue
        return scheduled
    return None


def _next_scheduled_departure(stop_id, route, after):
    """Return the first GTFS departure at a stop on or after ``after``."""
    departures = []
    for scheduled_route, scheduled_time, active_days in _scheduled_departures_by_stop().get(stop_id, []):
        if str(scheduled_route).casefold() != str(route).casefold():
            continue
        departure = _next_departure_datetime(scheduled_time, active_days, after)
        if departure is not None:
            departures.append(departure)
    return min(departures, default=None)


def _distance_meters(from_lat, from_lon, to_lat, to_lon):
    lat_delta = radians(to_lat - from_lat)
    lon_delta = radians(to_lon - from_lon)
    a = sin(lat_delta / 2) ** 2 + cos(radians(from_lat)) * cos(radians(to_lat)) * sin(lon_delta / 2) ** 2
    return 6371000 * 2 * asin(sqrt(a))


@lru_cache(maxsize=1)
def _rail_and_brt_stations():
    """Return the rail-feed stops, including the Sunway BRT stations."""
    osm_coordinates = _osm_station_coordinates()
    zip_path = DATA_DIRECTORY / "gtfs-rail.zip"
    if not zip_path.exists():
        return []
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            with archive.open("stops.txt") as file:
                return [
                    {
                        "id": row["stop_id"],
                        "name": row["stop_name"],
                        "lat": osm_coordinates.get(row["stop_id"], (float(row["stop_lat"]), float(row["stop_lon"])))[0],
                        "lon": osm_coordinates.get(row["stop_id"], (float(row["stop_lat"]), float(row["stop_lon"])))[1],
                    }
                    for row in csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig"))
                ]
    except (zipfile.BadZipFile, OSError, KeyError):
        return []


@lru_cache(maxsize=1)
def _osm_station_coordinates():
    """Use Supabase station coordinates first, then audited OSM coordinates."""
    try:
        with STATION_COORDINATE_AUDIT_FILE.open(encoding="utf-8") as source:
            coordinates = {
                item["stop_id"]: (item["osm_lat"], item["osm_lon"])
                for item in json.load(source)
                if item.get("osm_lat") is not None and item.get("osm_lon") is not None
            }
            coordinates.update(_supabase_station_coordinates())
            return coordinates
    except (OSError, ValueError, TypeError):
        return _supabase_station_coordinates()


@lru_cache(maxsize=1)
def _supabase_station_coordinates():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return {}
    try:
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/{SUPABASE_STATION_COORDINATES_TABLE}",
            params={"select": "stop_id,lat,lon"},
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
            timeout=4,
        )
        response.raise_for_status()
        return {
            str(row["stop_id"]): (float(row["lat"]), float(row["lon"]))
            for row in response.json()
            if row.get("stop_id") is not None
            and row.get("lat") is not None
            and row.get("lon") is not None
        }
    except (requests.exceptions.RequestException, TypeError, ValueError) as error:
        logger.warning("Could not read station coordinates from Supabase: %s", error)
        return {}


def _nearest_rail_or_brt_station(latitude, longitude):
    stations = _rail_and_brt_stations()
    if not stations:
        return None
    return min(
        stations,
        key=lambda station: _distance_meters(
            latitude, longitude, station["lat"], station["lon"]
        ),
    )


@lru_cache(maxsize=1)
def _all_transit_stations():
    """Return one nearest-access target for every GTFS stop/station."""
    osm_coordinates = _osm_station_coordinates()
    stations = {}
    for stop_id, name, latitude, longitude in _transit_stop_locations():
        latitude, longitude = osm_coordinates.get(stop_id, (latitude, longitude))
        stations.setdefault(stop_id, {
            "id": stop_id,
            "name": name,
            "lat": latitude,
            "lon": longitude,
        })
    return tuple(stations.values())


def _nearest_transit_station(latitude, longitude):
    stations = _all_transit_stations()
    if not stations:
        raise HTTPException(status_code=500, detail="Transit station database unavailable.")
    return min(
        stations,
        key=lambda station: _distance_meters(
            latitude, longitude, station["lat"], station["lon"]
        ),
    )


@lru_cache(maxsize=1)
def _motis_stop_id_index():
    """Map each local GTFS stop ID to MOTIS's dataset-qualified ID."""
    index = {}
    for filename in ("gtfs-bus.zip", "gtfs-mrtfeeder.zip", "gtfs-rail.zip", "gtfs-ktmb.zip"):
        dataset = Path(filename).stem
        try:
            with zipfile.ZipFile(DATA_DIRECTORY / filename) as archive:
                with archive.open("stops.txt") as file:
                    for row in csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig")):
                        stop_id = row.get("stop_id")
                        if stop_id:
                            index.setdefault(stop_id, f"{dataset}_{stop_id}")
        except (OSError, KeyError, zipfile.BadZipFile):
            continue
    return index


def _motis_stop_ref(stop_id):
    return _motis_stop_id_index().get(stop_id)


def _covered_walkway_segments():
    """Index OSM-covered pedestrian ways for quick itinerary matching."""
    global _covered_walkway_grid
    if _covered_walkway_grid is not None:
        return _covered_walkway_grid

    grid = defaultdict(list)
    try:
        with COVERED_WALKWAYS_FILE.open(encoding="utf-8") as source:
            for line in source:
                try:
                    feature = json.loads(line.lstrip("\x1e"))
                    coordinates = feature["geometry"]["coordinates"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
                for start, end in zip(coordinates, coordinates[1:]):
                    lon_1, lat_1 = start
                    lon_2, lat_2 = end
                    segment = (lon_1, lat_1, lon_2, lat_2)
                    min_lon, max_lon = sorted((lon_1, lon_2))
                    min_lat, max_lat = sorted((lat_1, lat_2))
                    for lon_cell in range(
                        int(min_lon // _COVERED_WALKWAY_GRID_SIZE),
                        int(max_lon // _COVERED_WALKWAY_GRID_SIZE) + 1,
                    ):
                        for lat_cell in range(
                            int(min_lat // _COVERED_WALKWAY_GRID_SIZE),
                            int(max_lat // _COVERED_WALKWAY_GRID_SIZE) + 1,
                        ):
                            grid[(lon_cell, lat_cell)].append(segment)
    except OSError:
        pass
    _covered_walkway_grid = grid
    return grid


def _decode_polyline(encoded):
    points = []
    index = latitude = longitude = 0
    while index < len(encoded):
        values = []
        for _ in range(2):
            shift = result = 0
            while index < len(encoded):
                value = ord(encoded[index]) - 63
                index += 1
                result |= (value & 0x1F) << shift
                shift += 5
                if value < 0x20:
                    break
            values.append(~(result >> 1) if result & 1 else result >> 1)
        latitude += values[0]
        longitude += values[1]
        points.append((latitude / 1e5, longitude / 1e5))
    return points


def _point_to_segment_meters(latitude, longitude, segment):
    lon_1, lat_1, lon_2, lat_2 = segment
    longitude_scale = 111320 * cos(radians(latitude))
    x_1, y_1 = (lon_1 - longitude) * longitude_scale, (lat_1 - latitude) * 110540
    x_2, y_2 = (lon_2 - longitude) * longitude_scale, (lat_2 - latitude) * 110540
    length_squared = x_2 * x_2 + y_2 * y_2
    if length_squared == 0:
        return sqrt(x_1 * x_1 + y_1 * y_1)
    position = max(0, min(1, -(x_1 * (x_2 - x_1) + y_1 * (y_2 - y_1)) / length_squared))
    nearest_x, nearest_y = x_1 + position * (x_2 - x_1), y_1 + position * (y_2 - y_1)
    return sqrt(nearest_x * nearest_x + nearest_y * nearest_y)


def _nearest_point_on_segment(latitude, longitude, segment):
    """Return the closest latitude/longitude point on an OSM walkway segment."""
    lon_1, lat_1, lon_2, lat_2 = segment
    longitude_scale = 111320 * cos(radians(latitude))
    x_1, y_1 = (lon_1 - longitude) * longitude_scale, (lat_1 - latitude) * 110540
    x_2, y_2 = (lon_2 - longitude) * longitude_scale, (lat_2 - latitude) * 110540
    length_squared = x_2 * x_2 + y_2 * y_2
    if length_squared == 0:
        return lat_1, lon_1
    position = max(0, min(1, -(x_1 * (x_2 - x_1) + y_1 * (y_2 - y_1)) / length_squared))
    return lat_1 + position * (lat_2 - lat_1), lon_1 + position * (lon_2 - lon_1)


def _station_access_target(from_lat, from_lon, station):
    station_distance = _distance_meters(
        from_lat, from_lon, station["lat"], station["lon"]
    )
    if station_distance <= STATION_AREA_RADIUS_METERS:
        return dict(station), True

    grid = _covered_walkway_segments()
    lon_cell = int(station["lon"] // _COVERED_WALKWAY_GRID_SIZE)
    lat_cell = int(station["lat"] // _COVERED_WALKWAY_GRID_SIZE)
    candidates = [
        segment
        for x in range(lon_cell - 2, lon_cell + 3)
        for y in range(lat_cell - 2, lat_cell + 3)
        for segment in grid.get((x, y), [])
        if _point_to_segment_meters(station["lat"], station["lon"], segment)
        <= STATION_WALKWAY_RADIUS_METERS
    ]
    if not candidates:
        return dict(station), False

    access_lat, access_lon = min(
        (_nearest_point_on_segment(from_lat, from_lon, segment) for segment in candidates),
        key=lambda point: _distance_meters(from_lat, from_lon, point[0], point[1]),
    )
    if _distance_meters(from_lat, from_lon, access_lat, access_lon) >= station_distance:
        return dict(station), False
    return {
        **station,
        "lat": access_lat,
        "lon": access_lon,
        "name": f"{station['name']} station area",
    }, False


def _sheltered_walk_fraction(leg):
    geometry = leg.get("legGeometry") or {}
    encoded = geometry.get("points")
    if not isinstance(encoded, str):
        return 0.0
    points = _decode_polyline(encoded)
    if len(points) < 2:
        return 0.0

    grid = _covered_walkway_segments()
    total_distance = sheltered_distance = 0.0
    for (lat_1, lon_1), (lat_2, lon_2) in zip(points, points[1:]):
        distance = _distance_meters(lat_1, lon_1, lat_2, lon_2)
        if distance == 0:
            continue
        total_distance += distance
        midpoint_lat, midpoint_lon = (lat_1 + lat_2) / 2, (lon_1 + lon_2) / 2
        lon_cell = int(midpoint_lon // _COVERED_WALKWAY_GRID_SIZE)
        lat_cell = int(midpoint_lat // _COVERED_WALKWAY_GRID_SIZE)
        candidates = (
            segment
            for x in range(lon_cell - 1, lon_cell + 2)
            for y in range(lat_cell - 1, lat_cell + 2)
            for segment in grid.get((x, y), [])
        )
        if any(
            _point_to_segment_meters(midpoint_lat, midpoint_lon, segment)
            <= _SHELTER_MATCH_DISTANCE_METERS
            for segment in candidates
        ):
            sheltered_distance += distance
    return sheltered_distance / total_distance if total_distance else 0.0


def _traffic_delay_factor(latitude, longitude):
    if not TOMTOM_API_KEY:
        return None

    cache_key = (round(latitude, 3), round(longitude, 3))
    cached = _traffic_cache.get(cache_key)
    if cached and monotonic() - cached["loaded_at"] < TRAFFIC_CACHE_SECONDS:
        return cached["factor"]

    try:
        response = requests.get(
            TOMTOM_FLOW_URL,
            params={"point": f"{latitude},{longitude}", "key": TOMTOM_API_KEY},
            timeout=3,
        )
        response.raise_for_status()
        flow = response.json().get("flowSegmentData", {})
        current = flow.get("currentTravelTime")
        free_flow = flow.get("freeFlowTravelTime")
        if not current or not free_flow:
            return None
        factor = max(0.7, min(float(current) / float(free_flow), 2.0))
        _traffic_cache[cache_key] = {"loaded_at": monotonic(), "factor": factor}
        return factor
    except (requests.exceptions.RequestException, TypeError, ValueError):
        return None


def _road_route(from_lat, from_lon, to_lat, to_lon):
    # MOTIS is the only routing engine. OSRM supplies a road geometry for the
    # separate e-hailing estimate because MOTIS transit planning is not used
    # for that leg.
    try:
        response = requests.get(
            f"{OSRM_DRIVING_URL}/{from_lon},{from_lat};{to_lon},{to_lat}",
            params={"overview": "full", "geometries": "geojson", "steps": "false"},
            timeout=8,
        )
        response.raise_for_status()
        route = (response.json().get("routes") or [None])[0]
        coordinates = ((route or {}).get("geometry") or {}).get("coordinates") or []
        if route and len(coordinates) >= 2:
            return {
                "duration": max(60, int(float(route.get("duration") or 0))),
                "coordinates": coordinates,
            }
    except (requests.exceptions.RequestException, TypeError, ValueError, IndexError):
        pass
    return None


def _fallback_itinerary(request, mode, message):
    direct_distance = _distance_meters(
        request.from_lat, request.from_lon, request.to_lat, request.to_lon
    )
    speed_mps = 1.35 if mode == "WALK" else 7.8
    road_route = None
    if mode == "HAIL":
        road_route = _road_route(
            request.from_lat, request.from_lon, request.to_lat, request.to_lon
        )
        if road_route is not None:
            direct_distance = sum(
                _distance_meters(a[1], a[0], b[1], b[0])
                for a, b in zip(road_route["coordinates"], road_route["coordinates"][1:])
            )
            duration = road_route["duration"]
        else:
            duration = max(60, int(direct_distance / speed_mps))
    else:
        duration = max(60, int(direct_distance / speed_mps))
    traffic_factor = None
    if mode == "HAIL":
        traffic_factor = _traffic_delay_factor(request.from_lat, request.from_lon)
        if traffic_factor is not None:
            duration = max(60, int(duration * traffic_factor))
    start = _local_now()
    end = start + timedelta(seconds=duration)
    itinerary = {
        "duration": duration,
        "fallback": {"type": mode.lower(), "message": message},
        "legs": [{
            "mode": mode,
            "startTime": str(int(start.timestamp() * 1000)),
            "endTime": str(int(end.timestamp() * 1000)),
            "headsign": "Your destination",
            "from": {"name": "Starting point", "lat": request.from_lat, "lon": request.from_lon},
            "to": {"name": "Your destination", "lat": request.to_lat, "lon": request.to_lon},
            "routeShortName": "Walk" if mode == "WALK" else "E-hailing estimate",
            "intermediateStops": [],
        }],
    }
    if road_route is not None:
        itinerary["legs"][0]["legGeometry"] = {
            "coordinates": road_route["coordinates"]
        }
        itinerary["legs"][0]["usesRoadRoute"] = True
    else:
        itinerary["legs"][0]["roadRoutingUnavailable"] = True
    if mode == "HAIL":
        itinerary["fare"] = {
            "amount": round((direct_distance / 1000) * EHAILING_RATE_PER_KM, 2),
            "currency": "MYR",
            "label": f"Estimated at RM{EHAILING_RATE_PER_KM:.2f}/km",
        }
    return itinerary


def _is_public_transport_leg(leg):
    return (leg.get("mode") or "").upper() in {"BUS", "RAIL", "SUBWAY", "TRAM"}


def _last_mile_ehailing_leg(from_place, to_lat, to_lon, start_time_ms):
    try:
        from_lat, from_lon = float(from_place["lat"]), float(from_place["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    distance = _distance_meters(from_lat, from_lon, to_lat, to_lon)
    duration = max(60, int(distance / 7.8))
    road_route = _road_route(from_lat, from_lon, to_lat, to_lon)
    if road_route is not None:
        distance = sum(
            _distance_meters(a[1], a[0], b[1], b[0])
            for a, b in zip(road_route["coordinates"], road_route["coordinates"][1:])
        )
        duration = road_route["duration"]
    traffic_factor = _traffic_delay_factor(from_lat, from_lon)
    if traffic_factor is not None:
        duration = max(60, int(duration * traffic_factor))
    leg = {
        "mode": "HAIL",
        "startTime": str(start_time_ms),
        "endTime": str(start_time_ms + duration * 1000),
        "headsign": "Your destination",
        "from": dict(from_place),
        "to": {"name": "Your destination", "lat": to_lat, "lon": to_lon},
        "routeShortName": "Last-mile e-hailing",
        "intermediateStops": [],
        "estimatedFare": round((distance / 1000) * EHAILING_RATE_PER_KM, 2),
        "lastMileEhailing": True,
    }
    if road_route is not None:
        leg["legGeometry"] = {"coordinates": road_route["coordinates"]}
        leg["usesRoadRoute"] = True
    else:
        leg["roadRoutingUnavailable"] = True
    return leg


def _replace_long_last_mile_walks(itineraries, request):
    for itinerary in itineraries:
        legs = itinerary.get("legs") or []
        transit_indices = [index for index, leg in enumerate(legs) if _is_public_transport_leg(leg)]
        if not transit_indices or any((leg.get("mode") or "").upper() == "HAIL" for leg in legs):
            continue
        last_transit_index = transit_indices[-1]
        tail = legs[last_transit_index + 1:]
        if not tail or any((leg.get("mode") or "").upper() != "WALK" for leg in tail):
            continue
        try:
            walk_seconds = sum(
                max(0, (int(leg["endTime"]) - int(leg["startTime"])) // 1000)
                for leg in tail
            )
            transit_end = int(legs[last_transit_index]["endTime"])
        except (KeyError, TypeError, ValueError):
            continue
        if walk_seconds * 1.35 <= LAST_MILE_EHAILING_THRESHOLD_METERS:
            continue
        hail_leg = _last_mile_ehailing_leg(
            legs[last_transit_index].get("to") or {},
            request.to_lat,
            request.to_lon,
            transit_end,
        )
        if hail_leg is None:
            continue
        itinerary["legs"] = legs[:last_transit_index + 1] + [hail_leg]
        itinerary["duration"] = max(
            0, itinerary.get("duration", 0) - walk_seconds
        ) + (int(hail_leg["endTime"]) - transit_end) // 1000
        itinerary["lastMileEhailing"] = True


def _discard_walks_over_one_kilometre(itineraries):
    valid = []
    for itinerary in itineraries:
        too_long = False
        for leg in itinerary.get("legs") or []:
            if (leg.get("mode") or "").upper() != "WALK":
                continue
            try:
                seconds = max(0, (int(leg["endTime"]) - int(leg["startTime"])) // 1000)
            except (KeyError, TypeError, ValueError):
                continue
            if seconds * 1.35 > 1000:
                too_long = True
                break
        if not too_long:
            valid.append(itinerary)
    return valid


def _street_walk_to_station(from_lat, from_lon, station, departure_date, departure_time):
    try:
        departure_datetime = datetime.strptime(
            f"{departure_date} {departure_time}", "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=APP_TIMEZONE)
        itinerary = motis_walk(
            from_lat=from_lat,
            from_lon=from_lon,
            to_lat=station["lat"],
            to_lon=station["lon"],
            departure_time=departure_datetime,
            timeout=20,
        )
        if not itinerary or not itinerary.get("legs"):
            return None
        for leg in itinerary["legs"]:
            leg["isNearestStationAccess"] = True
            leg["usesStreetAccess"] = True
        itinerary["legs"][-1]["to"] = {
            "name": station["name"],
            "lat": station["lat"],
            "lon": station["lon"],
        }
        return itinerary
    except (requests.exceptions.RequestException, IndexError, TypeError, ValueError):
        return None


def _prepend_origin_access(itinerary, access_itinerary):
    access_legs = copy.deepcopy(access_itinerary.get("legs") or [])
    transit_legs = itinerary.get("legs") or []
    if not access_legs or not transit_legs:
        return itinerary
    itinerary["legs"] = access_legs + transit_legs
    try:
        itinerary["duration"] = max(
            0,
            (int(transit_legs[-1]["endTime"]) - int(access_legs[0]["startTime"])) // 1000,
        )
    except (KeyError, TypeError, ValueError):
        pass
    itinerary["originStationAccess"] = True
    return itinerary


def _destination_access_leg(destination_station, to_lat, to_lon, access_itinerary):
    source_legs = (access_itinerary or {}).get("legs") or []
    if not source_legs:
        return None
    try:
        duration = sum(
            max(0, (int(leg["endTime"]) - int(leg["startTime"])) // 1000)
            for leg in source_legs
        )
    except (KeyError, TypeError, ValueError):
        return None
    coordinates = []
    for leg in source_legs:
        encoded = (leg.get("legGeometry") or {}).get("points")
        if isinstance(encoded, str):
            coordinates.extend(_decode_polyline(encoded))
    geometry = None
    if coordinates:
        geometry = {
            "coordinates": [[lon, lat] for lat, lon in reversed(coordinates)]
        }
    leg = {
        "mode": "WALK",
        "startTime": "0",
        "endTime": str(duration * 1000),
        "headsign": "",
        "from": dict(destination_station),
        "to": {"name": "Destination", "lat": to_lat, "lon": to_lon},
        "routeShortName": None,
        "intermediateStops": [],
        "isDestinationAccess": True,
        "usesStreetAccess": True,
    }
    if geometry:
        leg["legGeometry"] = geometry
    return leg


def _append_destination_access(itinerary, access_leg):
    if access_leg is None:
        return itinerary
    legs = itinerary.get("legs") or []
    if not legs:
        return itinerary
    try:
        start = int(legs[-1]["endTime"])
        duration = int(access_leg["endTime"]) // 1000
    except (KeyError, TypeError, ValueError):
        return itinerary
    access_leg = copy.deepcopy(access_leg)
    access_leg["startTime"] = str(start)
    access_leg["endTime"] = str(start + duration * 1000)
    itinerary["legs"] = legs + [access_leg]
    itinerary["duration"] = max(
        0, (int(access_leg["endTime"]) - int(legs[0]["startTime"])) // 1000
    )
    itinerary["destinationStationAccess"] = True
    return itinerary


def _terminal_station(trip):
    headsign = (trip.get("trip_headsign") or "").strip()
    if " to " in headsign.lower():
        return headsign.rsplit(" to ", 1)[-1].strip()
    return headsign


def _route_label(route):
    for field in ("shortName", "longName"):
        value = (route.get(field) or "").strip()
        if value:
            return value
    return None


@lru_cache(maxsize=1)
def _ktm_fare_table():
    try:
        with KTM_FARE_FILE.open(encoding="utf-8") as source:
            return json.load(source)
    except (OSError, ValueError, TypeError):
        return {}


def _fare_station_key(value):
    return re.sub(r"[^A-Z0-9]", "", unicodedata.normalize("NFKD", (value or "").upper()))


def _ktm_fare_station_index():
    table = _ktm_fare_table()
    names = table.get("stationNames") or []
    aliases = {
        "PELABUHANKLANG": "PELKLANG",
        "JALANKASTAM": "JLNKASTAM",
        "KAMPUNGRAJAUDA": "KGRAJAUDA",
        "KAMPUNGDATOHARUN": "KGDATOHARUN",
        "JALANTEMPLER": "JLNTEMPLER",
        "KAMPUNGBATU": "KGBATU",
        "BATUKENTONMEN": "BATUKENTONMEN",
        "TANJUNGMALIM": "TANJUNGMALIM",
    }
    index = {_fare_station_key(name): number for number, name in enumerate(names)}
    index.update({alias: index[target] for alias, target in aliases.items() if target in index})
    return index


def _ktm_fare_station_number(name):
    key = _fare_station_key(name)
    index = _ktm_fare_station_index()
    if key in index:
        return index[key]
    matches = {number for station_key, number in index.items() if station_key in key or key in station_key}
    return next(iter(matches)) if len(matches) == 1 else None


def _is_ktm_leg(leg):
    route = leg.get("route") or {}
    text = f"{route.get('shortName') or ''} {route.get('longName') or ''}".upper()
    return "KTM" in text or "KOMUTER" in text


def _ktm_fare_for_itinerary(itinerary):
    table = _ktm_fare_table()
    matrix = table.get("matrix") or []
    total = 0.0
    matched = False
    for leg in itinerary.get("legs") or []:
        if not _is_ktm_leg(leg):
            continue
        origin = _ktm_fare_station_number((leg.get("from") or {}).get("name"))
        destination = _ktm_fare_station_number((leg.get("to") or {}).get("name"))
        if origin is None or destination is None:
            continue
        try:
            fare = matrix[origin][destination]
        except (IndexError, TypeError):
            fare = None
        if fare is not None:
            total += float(fare)
            matched = True
    if not matched:
        return None
    return {
        "amount": round(total, 2),
        "currency": "MYR",
        "paymentType": "cash",
        "effectiveDate": table.get("effectiveDate"),
        "label": "KTM Komuter cash fare",
    }


def _payment_guidance_for_leg(leg):
    mode = (leg.get("mode") or "").upper()
    if mode == "HAIL":
        return "Pay in your selected e-hailing app"
    if _is_ktm_leg(leg):
        return "KTM Komuter: cash ticket or cashless payment"
    if mode == "BUS":
        return "Rapid KL bus: Touch 'n Go or MyRapid concession card"
    if mode in {"RAIL", "SUBWAY", "TRAM"}:
        return "Rapid KL rail/BRT: Touch 'n Go or a single-journey token"
    return None


@lru_cache(maxsize=1)
def _trip_stop_sequences():
    sequences = {}
    for filename in REALTIME_FEEDS:
        zip_path = DATA_DIRECTORY / filename
        if not zip_path.exists():
            continue
        try:
            with zipfile.ZipFile(zip_path, "r") as archive:
                def read_csv(name):
                    with archive.open(name) as file:
                        return list(csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig")))

                stops = {row["stop_id"]: row for row in read_csv("stops.txt")}
                routes = {row["route_id"]: row for row in read_csv("routes.txt")}
                trips = {row["trip_id"]: row for row in read_csv("trips.txt")}
                for row in sorted(read_csv("stop_times.txt"), key=lambda item: (item["trip_id"], int(item["stop_sequence"]))):
                    stop = stops.get(row["stop_id"])
                    if stop is None:
                        continue
                    trip = trips.get(row["trip_id"], {})
                    route = routes.get(trip.get("route_id", ""), {})
                    route_name = route.get("route_short_name") or route.get("route_long_name") or trip.get("route_id", "Transit")
                    is_brt = (
                        str(route_name).strip().casefold() == "brt"
                        or str(route.get("route_type", "")).strip().upper() == "BRT"
                    )
                    sequences.setdefault(row["trip_id"], {
                        "route": route_name,
                        "direction": _terminal_station(trip),
                        "is_bus": filename != "gtfs-rail.zip" or is_brt,
                        "stops": [],
                    })["stops"].append((row["stop_id"], float(stop["stop_lat"]), float(stop["stop_lon"])))
        except (zipfile.BadZipFile, KeyError, OSError):
            continue
    return sequences


@lru_cache(maxsize=1)
def _station_transfer_links():
    zip_path = DATA_DIRECTORY / "gtfs-rail.zip"
    if not zip_path.exists():
        return []
    try:
        with zipfile.ZipFile(zip_path) as archive:
            with archive.open("stops.txt") as file:
                stops = {
                    row["stop_id"]: row
                    for row in csv.DictReader(
                        io.TextIOWrapper(file, encoding="utf-8-sig")
                    )
                }
            if "transfers.txt" not in archive.namelist():
                return []
            with archive.open("transfers.txt") as file:
                transfers = list(
                    csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig"))
                )
        links = []
        for transfer in transfers:
            origin = stops.get(transfer.get("from_stop_id"))
            destination = stops.get(transfer.get("to_stop_id"))
            try:
                links.append((
                    float(origin["stop_lat"]),
                    float(origin["stop_lon"]),
                    float(destination["stop_lat"]),
                    float(destination["stop_lon"]),
                    int(transfer.get("min_transfer_time") or 60),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return links
    except (zipfile.BadZipFile, OSError, KeyError):
        return []


def _apply_station_transfer_times(itineraries):
    for itinerary in itineraries:
        legs = itinerary.get("legs") or []
        for index, leg in enumerate(legs):
            if (leg.get("mode") or "").upper() != "WALK":
                continue
            previous_mode = (
                (legs[index - 1].get("mode") or "").upper()
                if index else ""
            )
            next_mode = (
                (legs[index + 1].get("mode") or "").upper()
                if index + 1 < len(legs) else ""
            )
            if previous_mode == "" and next_mode in {"", "WALK"}:
                continue
            origin, destination = leg.get("from") or {}, leg.get("to") or {}
            try:
                origin_lat, origin_lon = float(origin["lat"]), float(origin["lon"])
                destination_lat = float(destination["lat"])
                destination_lon = float(destination["lon"])
                start_time, end_time = int(leg["startTime"]), int(leg["endTime"])
            except (KeyError, TypeError, ValueError):
                continue
            for from_lat, from_lon, to_lat, to_lon, transfer_seconds in _station_transfer_links():
                if (_distance_meters(origin_lat, origin_lon, from_lat, from_lon) > 150 or
                        _distance_meters(destination_lat, destination_lon, to_lat, to_lon) > 150):
                    continue
                old_seconds = max(0, (end_time - start_time) // 1000)
                if old_seconds <= transfer_seconds:
                    break
                leg["endTime"] = str(start_time + transfer_seconds * 1000)
                leg["isTransferWalk"] = True
                itinerary["duration"] = max(
                    0, itinerary.get("duration", 0) - old_seconds + transfer_seconds
                )
                break


def _attach_transfer_waits(itineraries):
    for itinerary in itineraries:
        previous_transit_end = None
        waits = []
        for leg in itinerary.get("legs") or []:
            if not _is_public_transport_leg(leg):
                continue
            try:
                start = int(leg["startTime"])
                end = int(leg["endTime"])
            except (KeyError, TypeError, ValueError):
                previous_transit_end = None
                continue
            if previous_transit_end is not None:
                gap_seconds = max(0, (start - previous_transit_end) // 1000)
                waits.append(gap_seconds)
            previous_transit_end = end

        max_wait = max(waits, default=0)
        itinerary["transferWaitSeconds"] = max_wait
        itinerary["hasExcessiveTransferWait"] = (
            max_wait > MAX_PUBLIC_TRANSPORT_TRANSFER_WAIT_SECONDS
        )


def _discard_excessive_transfer_waits(itineraries):
    return [
        itinerary for itinerary in itineraries
        if not itinerary.get("hasExcessiveTransferWait", False)
    ]


@lru_cache(maxsize=1)
def _bus_stop_locations():
    locations = []
    for filename in ("gtfs-bus.zip", "gtfs-mrtfeeder.zip", "gtfs-rail.zip"):
        zip_path = DATA_DIRECTORY / filename
        if not zip_path.exists():
            continue
        try:
            with zipfile.ZipFile(zip_path, "r") as archive:
                with archive.open("stops.txt") as file:
                    for row in csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig")):
                        try:
                            locations.append((
                                row["stop_id"],
                                float(row["stop_lat"]),
                                float(row["stop_lon"]),
                            ))
                        except (KeyError, TypeError, ValueError):
                            continue
        except (zipfile.BadZipFile, OSError, KeyError):
            continue
    return locations


@lru_cache(maxsize=1)
def _transit_stop_locations():
    locations = []
    for filename in ("gtfs-bus.zip", "gtfs-mrtfeeder.zip", "gtfs-rail.zip", "gtfs-ktmb.zip"):
        zip_path = DATA_DIRECTORY / filename
        if not zip_path.exists():
            continue
        try:
            with zipfile.ZipFile(zip_path, "r") as archive:
                with archive.open("stops.txt") as file:
                    for row in csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig")):
                        try:
                            locations.append((
                                row["stop_id"], row.get("stop_name", "Station"),
                                float(row["stop_lat"]), float(row["stop_lon"]),
                            ))
                        except (KeyError, TypeError, ValueError):
                            continue
        except (zipfile.BadZipFile, OSError, KeyError):
            continue
    return locations


def _station_id_at_place(place):
    try:
        latitude, longitude = float(place["lat"]), float(place["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    locations = _transit_stop_locations()
    if not locations:
        return None
    stop_id, name, stop_lat, stop_lon = min(
        locations,
        key=lambda stop: _distance_meters(latitude, longitude, stop[2], stop[3]),
    )
    if _distance_meters(latitude, longitude, stop_lat, stop_lon) > 180:
        return None
    return {"id": stop_id, "name": name}


def _recent_station_presence(stop_ids):
    if not stop_ids or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return {}
    
    sanitized_ids = [re.sub(r"[^A-Za-z0-9_\-]", "", str(s_id)) for s_id in stop_ids if s_id]
    if not sanitized_ids:
        return {}

    cutoff = (_local_now() - timedelta(
        minutes=STATION_PRESENCE_WINDOW_MINUTES
    )).isoformat()
    try:
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/{SUPABASE_PRESENCE_TABLE}",
            params={
                "select": "station_id",
                "station_id": f"in.({','.join(sanitized_ids)})",
                "observed_at": f"gte.{cutoff}",
            },
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
            timeout=4,
        )
        response.raise_for_status()
        counts = defaultdict(int)
        for row in response.json():
            if row.get("station_id") in sanitized_ids:
                counts[row["station_id"]] += 1
        return dict(counts)
    except (requests.exceptions.RequestException, TypeError, ValueError) as error:
        logger.warning("Could not read presence from Supabase: %s", error)
        return {}


def _station_activity_level(reports):
    if reports >= 10:
        return "high"
    if reports >= 3:
        return "moderate"
    return "low"


def _recent_incident_reports(stop_ids):
    if not stop_ids or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return {}

    sanitized_ids = [re.sub(r"[^A-Za-z0-9_\-]", "", str(s_id)) for s_id in stop_ids if s_id]
    if not sanitized_ids:
        return {}

    cutoff = (_local_now() - timedelta(
        minutes=INCIDENT_REPORT_WINDOW_MINUTES
    )).isoformat()
    try:
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/{SUPABASE_INCIDENT_REPORTS_TABLE}",
            params={
                "select": "station_id,report_type,target_type,service_route",
                "station_id": f"in.({','.join(sanitized_ids)})",
                "reported_at": f"gte.{cutoff}",
            },
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
            timeout=4,
        )
        response.raise_for_status()
        reports = defaultdict(list)
        for row in response.json():
            stop_id = row.get("station_id")
            report_type = row.get("report_type")
            if stop_id in sanitized_ids and report_type:
                reports[stop_id].append({
                    "type": str(report_type),
                    "targetType": str(row.get("target_type") or "station"),
                    "route": str(row.get("service_route") or "").strip() or None,
                })
        return dict(reports)
    except (requests.exceptions.RequestException, TypeError, ValueError):
        return {}


def _incident_penalty(report_types):
    severity = {
        "stuckTrain": 120,
        "missingBus": 100,
        "disruption": 120,
        "safety": 70,
        "crowding": 30,
        "busNotArrived": 100,
        "busCrowding": 30,
        "busBreakdown": 120,
        "busSafety": 70,
    }
    return sum(
        severity.get(
            report_type.get("type") if isinstance(report_type, dict) else report_type,
            0,
        )
        for report_type in report_types
    )


def _attach_congestion_metadata(itineraries):
    station_matches = {}
    for itinerary in itineraries:
        for leg in itinerary.get("legs") or []:
            if (leg.get("mode") or "").upper() in {"WALK", "HAIL", ""}:
                continue
            for place in (leg.get("from") or {}, leg.get("to") or {}):
                station = _station_id_at_place(place)
                if station is not None:
                    station_matches[station["id"]] = station
    stop_ids = list(station_matches)
    presence = _recent_station_presence(stop_ids)
    incidents = _recent_incident_reports(stop_ids)

    for itinerary in itineraries:
        stations = []
        seen_stations = set()
        for leg in itinerary.get("legs") or []:
            mode = (leg.get("mode") or "").upper()
            if mode in {"WALK", "HAIL", ""}:
                continue
            for place in (leg.get("from") or {}, leg.get("to") or {}):
                station = _station_id_at_place(place)
                if station is None or station["id"] in seen_stations:
                    continue
                seen_stations.add(station["id"])
                reports = presence.get(station["id"], 0)
                stations.append({
                    "id": station["id"],
                    "name": station["name"],
                    "level": _station_activity_level(reports),
                    "recentReports": reports,
                })

        station_penalty = max(
            ({"low": 0, "moderate": 1, "high": 2}[station["level"]] for station in stations),
            default=0,
        )
        itinerary["congestionScore"] = station_penalty * 20
        itinerary["congestion"] = {
            "stationActivity": stations,
            "stationSource": "anonymous_presence" if presence else "unavailable",
        }
        affected_incidents = []
        incident_penalty = 0
        for station in stations:
            report_types = incidents.get(station["id"], [])
            if not report_types:
                continue
            affected_incidents.append({
                "stationId": station["id"],
                "stationName": station["name"],
                "reportTypes": [report["type"] for report in report_types],
            })
            incident_penalty += _incident_penalty(report_types)
        itinerary["incidentScore"] = incident_penalty
        itinerary["incidents"] = {
            "reports": affected_incidents,
            "source": "anonymous_incident_reports" if incidents else "unavailable",
        }
        for leg in itinerary.get("legs") or []:
            if (leg.get("mode") or "").upper() in {"WALK", "HAIL", ""}:
                continue
            leg_incidents = []
            seen = set()
            for place in (leg.get("from") or {}, leg.get("to") or {}):
                station = _station_id_at_place(place)
                if station is None or station["id"] in seen:
                    continue
                seen.add(station["id"])
                for report in incidents.get(station["id"], []):
                    route = report.get("route")
                    leg_route = _route_label(leg.get("route") or {}) or ""
                    if route and route.casefold() != leg_route.casefold():
                        continue
                    leg_incidents.append({
                        "stationName": station["name"],
                        "type": report["type"],
                        "route": route,
                    })
            if leg_incidents:
                leg["incidentReports"] = leg_incidents


def _live_vehicle_estimates(stop_id):
    if monotonic() - _realtime_cache["loaded_at"] > 20:
        vehicles = []
        for filename, source_url in REALTIME_FEEDS.items():
            try:
                response = requests.get(source_url, timeout=8)
                response.raise_for_status()
                feed = gtfs_realtime_pb2.FeedMessage()
                feed.ParseFromString(response.content)
                vehicles.extend(
                    (filename, entity.vehicle)
                    for entity in feed.entity
                    if entity.HasField("vehicle")
                )
            except requests.exceptions.RequestException:
                continue
        _realtime_cache.update(loaded_at=monotonic(), vehicles=vehicles)

    estimates = []
    traffic_lookups_remaining = MAX_TOMTOM_LOOKUPS_PER_REQUEST
    now = _local_now()
    for feed_name, vehicle in _realtime_cache["vehicles"]:
        if vehicle.timestamp and abs(now.timestamp() - vehicle.timestamp) > 120:
            continue
        trip_id = vehicle.trip.trip_id
        sequence = _trip_stop_sequences().get(trip_id)
        if sequence is None or not vehicle.HasField("position"):
            continue
        stops = sequence["stops"]
        nearest_index = min(range(len(stops)), key=lambda index: _distance_meters(vehicle.position.latitude, vehicle.position.longitude, stops[index][1], stops[index][2]))
        target_indices = [index for index, stop in enumerate(stops) if stop[0] == stop_id and index >= nearest_index]
        if not target_indices:
            continue
        target_index = target_indices[0]
        distance = _distance_meters(vehicle.position.latitude, vehicle.position.longitude, stops[nearest_index][1], stops[nearest_index][2])
        for index in range(nearest_index, target_index):
            distance += _distance_meters(stops[index][1], stops[index][2], stops[index + 1][1], stops[index + 1][2])
        has_reported_speed = vehicle.position.speed > 1
        if has_reported_speed:
            speed = vehicle.position.speed
            if feed_name != "gtfs-rail.zip":
                speed /= 3.6
            speed = max(2.0, min(speed, 16.7))
        else:
            speed = 6.0
        traffic_factor = None
        if feed_name != "gtfs-rail.zip" and traffic_lookups_remaining:
            traffic_factor = _traffic_delay_factor(vehicle.position.latitude, vehicle.position.longitude)
            traffic_lookups_remaining -= 1
        traffic_multiplier = 1.0
        if traffic_factor is not None:
            traffic_multiplier = (
                1.0 + (traffic_factor - 1.0) * 0.35
                if has_reported_speed
                else traffic_factor
            )
        eta_seconds = int((distance / speed) * traffic_multiplier)
        if eta_seconds <= 7200:
            estimated_time = now + timedelta(seconds=eta_seconds)
            estimates.append({
                "route": sequence["route"],
                "direction": sequence["direction"],
                "is_bus": sequence["is_bus"],
                "time": estimated_time.strftime("%I:%M %p").lstrip("0"),
                "timestamp": int(estimated_time.timestamp() * 1000),
                "is_estimated": True,
                "is_traffic_adjusted": traffic_factor is not None,
            })
    return sorted(estimates, key=lambda item: item["timestamp"])


def _live_bus_estimate_for_leg(leg, route_name):
    mode = (leg.get("mode") or "").upper()
    is_brt = str(route_name).strip().casefold() in {"brt", "b1000"}
    if (mode != "BUS" and not (is_brt and mode in {"TRAM", "RAIL"})) or not route_name:
        return None
    origin = leg.get("from") or {}
    try:
        latitude = float(origin["lat"])
        longitude = float(origin["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    locations = _bus_stop_locations()
    if not locations:
        return None
    stop_id, stop_latitude, stop_longitude = min(
        locations,
        key=lambda stop: _distance_meters(latitude, longitude, stop[1], stop[2]),
    )
    if _distance_meters(latitude, longitude, stop_latitude, stop_longitude) > 150:
        return None

    normalized_route = str(route_name).strip().casefold()
    route_aliases = {normalized_route}
    if normalized_route in {"brt", "b1000"}:
        route_aliases.update({"brt", "b1000"})
    for estimate in _live_vehicle_estimates(stop_id):
        if (estimate["is_bus"] and
                str(estimate["route"]).strip().casefold() in route_aliases):
            return estimate
    return None


def _itinerary_category(itinerary):
    has_brt = any(
        (_route_label(leg.get("route") or {}) or "").strip().upper() in {"BRT", "B1000"}
        for leg in itinerary.get("legs") or []
    )
    if has_brt:
        return "bus"
    modes = [
        (leg.get("mode") or "").upper()
        for leg in itinerary.get("legs") or []
    ]
    rail_legs = sum(mode in {"RAIL", "SUBWAY", "TRAM"} for mode in modes)
    bus_legs = modes.count("BUS")
    if rail_legs and rail_legs >= bus_legs:
        return "rail"
    if bus_legs:
        return "bus"
    if "HAIL" in modes:
        return "ehailing"
    return "walking"


@lru_cache(maxsize=1)
def _brt_to_usj7_connections():
    connections = {}
    zip_path = DATA_DIRECTORY / "gtfs-rail.zip"
    if not zip_path.exists():
        return connections
    try:
        with zipfile.ZipFile(zip_path) as archive:
            def read_csv(name):
                with archive.open(name) as file:
                    return list(csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig")))
            routes = {row["route_id"]: row for row in read_csv("routes.txt")}
            trips = {row["trip_id"]: row for row in read_csv("trips.txt")}
            stops = {row["stop_id"]: row for row in read_csv("stops.txt")}
            shapes = defaultdict(list)
            for row in read_csv("shapes.txt"):
                try:
                    shapes[row["shape_id"]].append((
                        int(row["shape_pt_sequence"]),
                        float(row["shape_pt_lat"]),
                        float(row["shape_pt_lon"]),
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
            stop_times = defaultdict(list)
            for row in read_csv("stop_times.txt"):
                stop_times[row["trip_id"]].append(row)

        for trip_id, rows in stop_times.items():
            trip = trips.get(trip_id, {})
            route = routes.get(trip.get("route_id"), {})
            if (route.get("route_short_name") or "").upper() != "BRT":
                continue
            rows.sort(key=lambda row: int(row.get("stop_sequence") or 0))
            target_index = next(
                (index for index, row in enumerate(rows) if row.get("stop_id") == "BRT7"),
                None,
            )
            if target_index is None:
                continue
            target = rows[target_index]
            try:
                target_seconds = _gtfs_time_seconds(target["arrival_time"])
            except (KeyError, TypeError, ValueError):
                continue
            for row in rows[:target_index]:
                try:
                    travel_seconds = target_seconds - _gtfs_time_seconds(row["departure_time"])
                except (KeyError, TypeError, ValueError):
                    continue
                if travel_seconds > 0:
                    origin_stop = stops.get(row["stop_id"], {})
                    target_stop = stops.get("BRT7", {})
                    shape = sorted(shapes.get(trip.get("shape_id"), []))
                    try:
                        origin_lat, origin_lon = float(origin_stop["stop_lat"]), float(origin_stop["stop_lon"])
                        target_lat, target_lon = float(target_stop["stop_lat"]), float(target_stop["stop_lon"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if shape:
                        start_index = min(
                            range(len(shape)),
                            key=lambda index: _distance_meters(origin_lat, origin_lon, shape[index][1], shape[index][2]),
                        )
                        end_index = min(
                            range(len(shape)),
                            key=lambda index: _distance_meters(target_lat, target_lon, shape[index][1], shape[index][2]),
                        )
                        segment = shape[start_index:end_index + 1]
                        coordinates = [[lon, lat] for _, lat, lon in segment] if end_index >= start_index else []
                    else:
                        coordinates = []
                    intermediate_stops = []
                    start_sequence = int(row.get("stop_sequence") or 0)
                    for stop_time in rows:
                        try:
                            sequence = int(stop_time.get("stop_sequence") or 0)
                        except ValueError:
                            continue
                        if not start_sequence < sequence < int(target.get("stop_sequence") or 0):
                            continue
                        stop = stops.get(stop_time.get("stop_id"), {})
                        try:
                            intermediate_stops.append({
                                "name": stop["stop_name"],
                                "lat": float(stop["stop_lat"]),
                                "lon": float(stop["stop_lon"]),
                            })
                        except (KeyError, TypeError, ValueError):
                            continue
                    connection = {
                        "travelSeconds": travel_seconds,
                        "coordinates": coordinates,
                        "intermediateStops": intermediate_stops,
                    }
                    previous = connections.get(row["stop_id"])
                    if previous is None or travel_seconds < previous["travelSeconds"]:
                        connections[row["stop_id"]] = connection
        return connections
    except (zipfile.BadZipFile, OSError, KeyError):
        return {}


def _gtfs_time_seconds(value):
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _add_brt_usj7_alternatives(itineraries, origin_station):
    if not origin_station or not origin_station.get("id"):
        return
    brt_connection = _brt_to_usj7_connections().get(origin_station.get("id"))
    if brt_connection is None:
        return
    brt_travel_seconds = brt_connection["travelSeconds"]
    brt_destination = {
        "name": "USJ7",
        "lat": 3.0548355,
        "lon": 101.591941,
    }
    alternatives = []
    for itinerary in itineraries:
        legs = itinerary.get("legs") or []
        if any(
            ((leg.get("route") or {}).get("shortName") or "").upper() == "BRT"
            for leg in legs
        ):
            continue
        for index, leg in enumerate(legs[:-1]):
            next_leg = legs[index + 1]
            if (leg.get("mode") or "").upper() != "WALK":
                continue
            if (next_leg.get("mode") or "").upper() not in {"RAIL", "SUBWAY"}:
                continue
            destination = leg.get("to") or {}
            try:
                reaches_usj7 = _distance_meters(
                    float(destination["lat"]),
                    float(destination["lon"]),
                    brt_destination["lat"],
                    brt_destination["lon"],
                ) <= 150
                start_time = int(leg["startTime"])
            except (KeyError, TypeError, ValueError):
                continue
            if not reaches_usj7:
                continue
            access_time = datetime.fromtimestamp(start_time / 1000, APP_TIMEZONE)
            brt_departure = _next_scheduled_departure(origin_station["id"], "BRT", access_time)
            if brt_departure is None:
                continue
            brt_arrival = brt_departure + timedelta(seconds=brt_travel_seconds)
            lrt_route = _route_label(next_leg.get("route") or {})
            lrt_departure = _next_scheduled_departure("KJ31", lrt_route, brt_arrival)
            if lrt_departure is None:
                continue
            transfer_wait = (lrt_departure - brt_arrival).total_seconds()
            if transfer_wait > MAX_PUBLIC_TRANSPORT_TRANSFER_WAIT_SECONDS:
                continue
            candidate = copy.deepcopy(itinerary)
            candidate_legs = candidate.get("legs") or []
            candidate_next_leg = candidate_legs[index + 1]
            try:
                old_lrt_start = int(next_leg["startTime"])
                old_lrt_end = int(next_leg["endTime"])
            except (KeyError, TypeError, ValueError):
                continue
            lrt_duration = max(0, (old_lrt_end - old_lrt_start) // 1000)
            brt_start = int(brt_departure.timestamp() * 1000)
            lrt_start = int(lrt_departure.timestamp() * 1000)
            candidate_legs[index] = {
                "mode": "TRAM",
                "startTime": str(brt_start),
                "endTime": str(brt_start + brt_travel_seconds * 1000),
                "headsign": "USJ7",
                "from": {
                    "name": origin_station["name"],
                    "lat": origin_station["lat"],
                    "lon": origin_station["lon"],
                },
                "to": brt_destination,
                "route": {"shortName": "BRT"},
                "legGeometry": {"coordinates": brt_connection["coordinates"]},
                "intermediateStops": brt_connection["intermediateStops"],
            }
            candidate_next_leg["startTime"] = str(lrt_start)
            candidate_next_leg["endTime"] = str(lrt_start + lrt_duration * 1000)
            candidate["duration"] = max(0, (
                max(int(item.get("endTime", 0)) for item in candidate_legs)
                - min(int(item.get("startTime", 0)) for item in candidate_legs)
            ) // 1000)
            alternatives.append(candidate)
            break
    itineraries.extend(alternatives)


def _enrich_brt_legs(itineraries):
    connections = _brt_to_usj7_connections()
    for itinerary in itineraries:
        for leg in itinerary.get("legs") or []:
            if (_route_label(leg.get("route") or {}) or "").upper() != "BRT":
                continue
            origin = _station_id_at_place(leg.get("from") or {})
            destination = _station_id_at_place(leg.get("to") or {})
            if origin is None or destination is None or destination["id"] != "BRT7":
                continue
            connection = connections.get(origin["id"])
            if connection is None:
                continue
            if not leg.get("intermediateStops"):
                leg["intermediateStops"] = copy.deepcopy(connection["intermediateStops"])
            if connection["coordinates"]:
                leg["legGeometry"] = {"coordinates": connection["coordinates"]}


@app.get("/api/transit/stops/{stop_id}/departures")
def get_next_departures(stop_id: str, limit: int | None = None):
    now = _local_now()
    next_departures = _live_vehicle_estimates(stop_id)

    frequency_departures = _frequency_departures(stop_id, now)
    frequency_routes = {
        departure["route"] for departure in frequency_departures
    }
    next_departures.extend(frequency_departures)

    for route, scheduled_time, active_days in _scheduled_departures_by_stop().get(stop_id, []):
        if route in frequency_routes:
            continue
        departure = _next_departure_datetime(scheduled_time, active_days, now)
        if departure is None:
            continue
        terminals = _scheduled_rail_terminals().get(
            (stop_id, route, scheduled_time, active_days), {""}
        )
        for terminal in terminals:
            next_departures.append({
                "route": route,
                "direction": terminal,
                "is_bus": bool(not terminal),
                "time": departure.strftime("%I:%M %p").lstrip("0"),
                "timestamp": int(departure.timestamp() * 1000),
                "is_estimated": False,
            })

    next_departures.sort(key=lambda item: item["timestamp"])
    unique_departures = []
    seen_departures = set()
    for departure in next_departures:
        key = (departure["route"],) if departure["is_bus"] else (
            departure["route"], departure["direction"]
        )
        if key not in seen_departures:
            unique_departures.append(departure)
            seen_departures.add(key)

    if limit is not None:
        limit = max(1, limit)
        unique_departures = unique_departures[:limit]
    return {"departures": unique_departures}


@app.get("/api/transit/stops/{stop_id}/incidents")
def get_stop_incidents(stop_id: str):
    reports = _recent_incident_reports([stop_id]).get(stop_id, [])
    counts = defaultdict(int)
    for report in reports:
        counts[(report["type"], report.get("route"))] += 1
    return {
        "incidents": [
            {"type": report_type, "route": route, "count": count}
            for (report_type, route), count in sorted(
                counts.items(), key=lambda item: (item[0][0], item[0][1] or "")
            )
        ],
        "windowMinutes": INCIDENT_REPORT_WINDOW_MINUTES,
    }


@lru_cache(maxsize=1)
def _stop_route_labels():
    labels = defaultdict(set)
    for filename in ("gtfs-bus.zip", "gtfs-mrtfeeder.zip", "gtfs-rail.zip", "gtfs-ktmb.zip"):
        zip_path = DATA_DIRECTORY / filename
        if not zip_path.exists():
            continue
        try:
            with zipfile.ZipFile(zip_path, "r") as archive:
                def read_csv(name):
                    with archive.open(name) as file:
                        return list(csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig")))
                routes = {row["route_id"]: row for row in read_csv("routes.txt")}
                trips = {row["trip_id"]: row for row in read_csv("trips.txt")}
                for row in read_csv("stop_times.txt"):
                    trip = trips.get(row.get("trip_id"), {})
                    route = routes.get(trip.get("route_id"), {})
                    label = route.get("route_short_name") or route.get("route_long_name")
                    if row.get("stop_id") and label:
                        labels[row["stop_id"]].add(str(label))
        except (zipfile.BadZipFile, OSError, KeyError):
            continue
    return {stop_id: ",".join(sorted(values)) for stop_id, values in labels.items()}


@app.get("/api/transit/stops")
def get_gtfs_stops():
    gtfs_files = [
        DATA_DIRECTORY / "gtfs-bus.zip",
        DATA_DIRECTORY / "gtfs-mrtfeeder.zip",
        DATA_DIRECTORY / "gtfs-rail.zip",
        DATA_DIRECTORY / "gtfs-ktmb.zip",
    ]
    features = []
    route_labels = _stop_route_labels()
    
    for zip_path in gtfs_files:
        if not zip_path.exists():
            continue
        try:
            with zipfile.ZipFile(zip_path, 'r') as z:
                if 'stops.txt' not in z.namelist():
                    continue
                    
                is_rail_dataset = any(name in zip_path.name.lower() for name in ("rail", "ktmb"))
                
                with z.open('stops.txt') as f:
                    text_stream = io.TextIOWrapper(f, encoding='utf-8-sig')
                    reader = csv.DictReader(text_stream)
                    
                    for row in reader:
                        try:
                            stop_lat = float(row['stop_lat'])
                            stop_lon = float(row['stop_lon'])
                            stop_name = row.get('stop_name', 'Unknown Stop')
                            stop_id = row.get('stop_id', '')
                            is_brt_stop = "BRT" in route_labels.get(stop_id, "").upper().split(",")
                            
                            feature = {
                                "type": "Feature",
                                "geometry": {
                                    "type": "Point",
                                    "coordinates": [stop_lon, stop_lat]
                                },
                                "properties": {
                                    "id": stop_id,
                                    "name": stop_name,
                                    "transit_type": "bus" if is_brt_stop else ("rail" if is_rail_dataset else "bus"),
                                    "routes": route_labels.get(stop_id, ""),
                                }
                            }
                            features.append(feature)
                        except (ValueError, KeyError):
                            continue
        except (zipfile.BadZipFile, OSError):
            continue
            
    if not features:
        raise HTTPException(status_code=500, detail="No GTFS data source packages found.")
        
    return {
        "type": "FeatureCollection",
        "features": features
    }


@app.post("/api/route/motis")
def get_motis_route(request: RouteRequest):
    """Smoke-test the MOTIS adapter without changing the production router."""
    departure = _local_now()
    departure_date = request.departure_date or departure.strftime("%Y-%m-%d")
    departure_time = request.departure_time or departure.strftime("%H:%M:%S")
    try:
        departure_datetime = datetime.strptime(
            f"{departure_date} {departure_time}", "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=APP_TIMEZONE)
        return {
            "itineraries": motis_plan(
                from_lat=request.from_lat,
                from_lon=request.from_lon,
                to_lat=request.to_lat,
                to_lon=request.to_lon,
                departure_time=departure_datetime,
            )
        }
    except requests.exceptions.RequestException as error:
        raise HTTPException(status_code=502, detail=f"MOTIS unavailable: {error}")
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=502, detail=f"Invalid MOTIS response: {error}")


@app.post("/api/route")
def get_transit_route(request: RouteRequest):
    departure = _local_now()
    departure_date = request.departure_date or departure.strftime("%Y-%m-%d")
    departure_time = request.departure_time or departure.strftime("%H:%M:%S")
    origin_station = _nearest_transit_station(request.from_lat, request.from_lon)
    destination_station = _nearest_transit_station(request.to_lat, request.to_lon)
    origin_station_distance = _distance_meters(
        request.from_lat,
        request.from_lon,
        origin_station["lat"],
        origin_station["lon"],
    )

    if origin_station_distance <= STATION_AREA_RADIUS_METERS:
        origin_access = None
        origin_access_allowed = True
    else:
        origin_access = _street_walk_to_station(
            request.from_lat,
            request.from_lon,
            origin_station,
            departure_date,
            departure_time,
        )
        origin_access_allowed = False

    routing_date, routing_time = departure_date, departure_time
    if origin_access and origin_access.get("legs"):
        try:
            access_seconds = sum(
                max(0, (int(leg["endTime"]) - int(leg["startTime"])) // 1000)
                for leg in origin_access["legs"]
            )
            origin_access_allowed = access_seconds * 1.35 <= 1000
            if origin_access_allowed:
                access_end = int(origin_access["legs"][-1]["endTime"])
                access_datetime = datetime.fromtimestamp(access_end / 1000, APP_TIMEZONE)
                routing_date = access_datetime.strftime("%Y-%m-%d")
                routing_time = access_datetime.strftime("%H:%M:%S")
        except (KeyError, TypeError, ValueError, IndexError, OSError):
            origin_access_allowed = False

    destination_station_distance = _distance_meters(
        request.to_lat,
        request.to_lon,
        destination_station["lat"],
        destination_station["lon"],
    )
    destination_access_leg = None
    destination_access_allowed = True
    if destination_station_distance > STATION_AREA_RADIUS_METERS:
        reverse_destination_walk = _street_walk_to_station(
            request.to_lat,
            request.to_lon,
            destination_station,
            departure_date,
            departure_time,
        )
        destination_access_leg = _destination_access_leg(
            destination_station,
            request.to_lat,
            request.to_lon,
            reverse_destination_walk,
        )
        if destination_access_leg is None:
            destination_access_allowed = False
        else:
            destination_seconds = int(destination_access_leg["endTime"]) // 1000
            destination_access_allowed = destination_seconds * 1.35 <= 1000

    if origin_station_distance <= STATION_AREA_RADIUS_METERS:
        routing_from_lat = request.from_lat
        routing_from_lon = request.from_lon
    else:
        routing_from_lat = origin_station["lat"]
        routing_from_lon = origin_station["lon"]
    routing_to_lat = destination_station["lat"]
    routing_to_lon = destination_station["lon"]

    try:
        departure_datetime = datetime.strptime(
            f"{routing_date} {routing_time}", "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=APP_TIMEZONE)
        itineraries = motis_plan(
            from_lat=routing_from_lat,
            from_lon=routing_from_lon,
            to_lat=routing_to_lat,
            to_lon=routing_to_lon,
            departure_time=departure_datetime,
            from_stop_id=_motis_stop_ref(origin_station["id"]),
            to_stop_id=_motis_stop_ref(destination_station["id"]),
        )
        if not origin_access_allowed or not destination_access_allowed:
            itineraries = []
        else:
            for itinerary in itineraries:
                if origin_access:
                    _prepend_origin_access(itinerary, origin_access)
                if destination_access_leg:
                    _append_destination_access(itinerary, destination_access_leg)
            try:
                _enrich_brt_legs(itineraries)
                _add_brt_usj7_alternatives(itineraries, origin_station)
            except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as error:
                logger.warning("BRT enrichment was unavailable: %s", error)
        _apply_station_transfer_times(itineraries)
        _attach_transfer_waits(itineraries)
        itineraries = _discard_excessive_transfer_waits(itineraries)
        _replace_long_last_mile_walks(itineraries, request)
        itineraries = _discard_walks_over_one_kilometre(itineraries)

        for itinerary in itineraries:
            sheltered_walk_fraction = 0.0
            walking_seconds = 0
            sheltered_walk_seconds = 0.0
            legs = itinerary.get("legs") or []
            for index, leg in enumerate(legs):
                if leg.get("mode", "").upper() != "WALK":
                    continue
                previous_mode = legs[index - 1].get("mode", "").upper() if index else ""
                next_mode = legs[index + 1].get("mode", "").upper() if index + 1 < len(legs) else ""
                leg["isTransferWalk"] = (
                    previous_mode not in {"", "WALK", "HAIL"}
                    and next_mode not in {"", "WALK", "HAIL"}
                )
                fraction = _sheltered_walk_fraction(leg)
                leg["isSheltered"] = fraction >= 0.35
                sheltered_walk_fraction += fraction
                try:
                    leg_walking_seconds = max(
                        0,
                        (int(leg.get("endTime", 0)) - int(leg.get("startTime", 0))) // 1000,
                    )
                except (TypeError, ValueError):
                    leg_walking_seconds = 0
                walking_seconds += leg_walking_seconds
                sheltered_walk_seconds += leg_walking_seconds * fraction
            itinerary["shelteredWalkScore"] = sheltered_walk_fraction
            itinerary["walkingSeconds"] = walking_seconds
            itinerary["shelteredWalkSeconds"] = sheltered_walk_seconds
            itinerary["uncoveredWalkSeconds"] = max(
                0, walking_seconds - sheltered_walk_seconds
            )

        _attach_congestion_metadata(itineraries)

        itineraries.sort(
            key=lambda itinerary: (
                itinerary.get("incidentScore", 0),
                itinerary["walkingSeconds"],
                itinerary["uncoveredWalkSeconds"],
                itinerary.get("congestionScore", 0),
                itinerary.get("duration", float("inf")),
            )
        )
        unique_itineraries = []
        seen_services = set()
        for itinerary in itineraries:
            services = tuple(
                (leg.get("route") or {}).get("shortName") or leg.get("mode")
                for leg in itinerary.get("legs") or []
                if leg.get("mode", "").upper() not in {"WALK", "HAIL"}
            )
            if services not in seen_services:
                unique_itineraries.append(itinerary)
                seen_services.add(services)

        for itinerary in unique_itineraries:
            itinerary["routeCategory"] = _itinerary_category(itinerary)

        transit_candidates = [
            itinerary for itinerary in unique_itineraries
            if any(
                (leg.get("mode") or "").upper() not in {"WALK", "HAIL", ""}
                for leg in itinerary.get("legs") or []
            )
        ]
        non_transit_candidates = [
            itinerary for itinerary in unique_itineraries
            if itinerary not in transit_candidates
        ]
        itineraries = (transit_candidates + non_transit_candidates)[:8]

        has_transit = any(
            leg.get("mode", "").upper() not in {"WALK", ""}
            for itinerary in itineraries
            for leg in itinerary.get("legs") or []
        )

        if has_transit:
            itineraries.append(_fallback_itinerary(
                request,
                "HAIL",
                "Approximate direct e-hailing journey.",
            ))
        else:
            if itineraries:
                itineraries[0]["fallback"] = {
                    "type": "walk",
                    "message": "No public transport is available; showing the walking route.",
                }
            else:
                direct_distance = _distance_meters(
                    request.from_lat, request.from_lon, request.to_lat, request.to_lon
                )
                if direct_distance <= 2000:
                    itineraries = [_fallback_itinerary(
                        request,
                        "WALK",
                        "No public transport is available; this is an approximate walking route.",
                    )]
                else:
                    itineraries = [_fallback_itinerary(
                        request,
                        "HAIL",
                        "No public transport is available; showing an approximate e-hailing journey.",
                    )]

        for itinerary in itineraries:
            ktm_fare = _ktm_fare_for_itinerary(itinerary)
            if ktm_fare is not None:
                itinerary["fare"] = ktm_fare
            itinerary["routeCategory"] = _itinerary_category(itinerary)
            for leg in itinerary.get("legs") or []:
                route_info = leg.get("route") or {}
                route_label = _route_label(route_info)
                leg["routeShortName"] = route_label
                leg["headsign"] = _terminal_station({
                    "trip_headsign": leg.get("headsign")
                })
                leg["isSheltered"] = bool(leg.get("isSheltered"))
                leg["paymentMethod"] = _payment_guidance_for_leg(leg)

                live_bus = _live_bus_estimate_for_leg(
                    leg, route_label
                )
                if live_bus is not None:
                    leg["liveBusEstimate"] = {
                        "timestamp": live_bus["timestamp"],
                        "trafficAdjusted": live_bus["is_traffic_adjusted"],
                    }

                intermediate_stops = []
                for stop in leg.get("intermediateStops") or []:
                    name = stop.get("name")
                    lat = stop.get("lat")
                    lon = stop.get("lon")
                    if name is not None and lat is not None and lon is not None:
                        intermediate_stops.append({
                            "name": name,
                            "lat": lat,
                            "lon": lon,
                        })
                leg["intermediateStops"] = intermediate_stops

        return {"itineraries": itineraries}
        
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Could not connect to MOTIS: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
