from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
import requests
import os
import zipfile
import io
import csv
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

from google.transit import gtfs_realtime_pb2

app = FastAPI(title="JomNaik Routing Middleware")

# Hardcoded OTP v2 GraphQL Endpoint
OTP_URL = "http://localhost:8080/otp/routers/default/index/graphql"
DATA_DIRECTORY = Path(__file__).parent
DEPARTURE_SCHEDULES_FILE = DATA_DIRECTORY / "departure_schedules.json"
COVERED_WALKWAYS_FILE = DATA_DIRECTORY / "covered_walkways.geojsonseq"
STATION_COORDINATE_AUDIT_FILE = DATA_DIRECTORY / "station_coordinate_audit.json"
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


TOMTOM_API_KEY = _configured_value("TOMTOM_API_KEY")
TOMTOM_FLOW_URL = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
SUPABASE_URL = _configured_value("SUPABASE_URL").rstrip("/")
# Keep this key on the server only. It is used to read anonymous, aggregated
# station-presence samples; the Flutter app continues to use its public key.
SUPABASE_SERVICE_ROLE_KEY = _configured_value("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_STATION_COORDINATES_TABLE = _configured_value(
    "SUPABASE_STATION_COORDINATES_TABLE"
) or "station_coordinates"
SUPABASE_PRESENCE_TABLE = _configured_value("SUPABASE_PRESENCE_TABLE") or "anonymous_station_presence"
_traffic_cache = {}
TRAFFIC_CACHE_SECONDS = 60
MAX_TOMTOM_LOOKUPS_PER_REQUEST = 6
STATION_PRESENCE_WINDOW_MINUTES = 15
EHAILING_RATE_PER_KM = 1.50
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
RAPIDKL_FARE_TABLE_URL = "https://mrt.com.my/fare/fares-master10.htm"
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
    prefer_brt: bool = False


@app.get("/api/health")
def health_check():
    return {"status": "ok"}


@app.get("/api/places/search")
def search_places(query: str = Query(min_length=2, max_length=120)):
    """Search for a place after an explicit user submission, not autocomplete."""
    global _last_geocode_request_at
    normalized_query = " ".join(query.split())
    cache_key = normalized_query.casefold()
    cached = _geocode_cache.get(cache_key)
    if cached is not None:
        return {"results": cached}

    # Respect the public Nominatim service's one-request-per-second policy.
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
        # A dropped pin remains useful even if a nearby OSM address is absent.
        return {
            "name": "Dropped pin",
            "address": f"{latitude:.5f}, {longitude:.5f}",
            "lat": latitude,
            "lon": longitude,
        }


@lru_cache(maxsize=1)
def _scheduled_departures_by_stop():
    with DEPARTURE_SCHEDULES_FILE.open(encoding="utf-8") as file:
        return json.load(file)


@lru_cache(maxsize=1)
def _scheduled_rail_terminals():
    """Map scheduled rail/BRT calls to their GTFS destination terminals."""
    terminals = {}
    for filename in ("gtfs-rail.zip", "gtfs-ktmb.zip"):
        with zipfile.ZipFile(DATA_DIRECTORY / filename, "r") as archive:
            def read_csv(name):
                with archive.open(name) as file:
                    return list(csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig")))

            routes = {row["route_id"]: row for row in read_csv("routes.txt")}
            trips = {row["trip_id"]: row for row in read_csv("trips.txt")}
            service_days = {
                row["service_id"]: "".join(
                    row[day] for day in (
                        "monday", "tuesday", "wednesday", "thursday",
                        "friday", "saturday", "sunday",
                    )
                )
                for row in read_csv("calendar.txt")
            }

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
    return terminals


def _next_departure_datetime(time_value, active_days, now):
    try:
        hours, minutes, seconds = (int(value) for value in time_value.split(":"))
    except ValueError:
        return None

    for day_offset in range(8):
        scheduled = now.replace(hour=0, minute=0, second=0, microsecond=0)
        scheduled += timedelta(days=day_offset, hours=hours, minutes=minutes, seconds=seconds)
        if active_days[scheduled.weekday()] != "1" or scheduled < now:
            continue
        return scheduled
    return None


def _distance_meters(from_lat, from_lon, to_lat, to_lon):
    lat_delta = radians(to_lat - from_lat)
    lon_delta = radians(to_lon - from_lon)
    a = sin(lat_delta / 2) ** 2 + cos(radians(from_lat)) * cos(radians(to_lat)) * sin(lon_delta / 2) ** 2
    return 6371000 * 2 * asin(sqrt(a))


@lru_cache(maxsize=1)
def _rail_and_brt_stations():
    """Return the rail-feed stops, including the Sunway BRT stations."""
    osm_coordinates = _osm_station_coordinates()
    with zipfile.ZipFile(DATA_DIRECTORY / "gtfs-rail.zip", "r") as archive:
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
            # Deployment data can correct a coordinate without requiring a
            # mobile release or an OTP-data repository update.
            coordinates.update(_supabase_station_coordinates())
            return coordinates
    except (OSError, ValueError, TypeError):
        return _supabase_station_coordinates()


@lru_cache(maxsize=1)
def _supabase_station_coordinates():
    """Load optional station overrides maintained in Supabase.

    The table shape is ``stop_id text primary key, lat double precision,
    lon double precision``. Missing credentials or table leave routing on the
    checked-in OSM audit, so route search remains available offline.
    """
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
    except (requests.exceptions.RequestException, TypeError, ValueError):
        return {}


def _nearest_rail_or_brt_station(latitude, longitude):
    return min(
        _rail_and_brt_stations(),
        key=lambda station: _distance_meters(
            latitude, longitude, station["lat"], station["lon"]
        ),
    )


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
    """Return a bounded road-congestion multiplier, or None when unavailable."""
    if not TOMTOM_API_KEY:
        return None

    # Rounding groups nearby vehicles onto one cached road-flow lookup.
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
        # Keep a bad or unusual road segment from producing implausible ETAs.
        factor = max(0.7, min(float(current) / float(free_flow), 2.0))
        _traffic_cache[cache_key] = {"loaded_at": monotonic(), "factor": factor}
        return factor
    except (requests.exceptions.RequestException, TypeError, ValueError):
        return None


def _fallback_itinerary(request, mode, message):
    """Create an honest, direct fallback when OTP has no route at all."""
    direct_distance = _distance_meters(
        request.from_lat, request.from_lon, request.to_lat, request.to_lon
    )
    speed_mps = 1.35 if mode == "WALK" else 7.8
    duration = max(60, int(direct_distance / speed_mps))
    traffic_factor = None
    if mode == "HAIL":
        traffic_factor = _traffic_delay_factor(request.from_lat, request.from_lon)
        if traffic_factor is not None:
            duration = max(60, int(duration * traffic_factor))
    start = datetime.now().astimezone()
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
    if mode == "HAIL":
        itinerary["fare"] = {
            "amount": round((direct_distance / 1000) * EHAILING_RATE_PER_KM, 2),
            "currency": "MYR",
            "label": f"Estimated at RM{EHAILING_RATE_PER_KM:.2f}/km",
        }
    return itinerary


def _street_walk_to_station(from_lat, from_lon, station, departure_date, departure_time):
    """Ask OTP for a network walk, rather than drawing a direct access line.

    OTP snaps the supplied point (including a point inside a building) to the
    closest walkable street/path vertex and returns the walkable geometry to
    the station.  ``None`` means the street graph could not reach the station.
    """
    query = """
    query StreetWalk($fromLat: Float!, $fromLon: Float!, $toLat: Float!, $toLon: Float!, $date: String!, $time: String!) {
      plan(
        from: { lat: $fromLat, lon: $fromLon }
        to: { lat: $toLat, lon: $toLon }
        date: $date
        time: $time
        numItineraries: 1
        optimize: SAFE
        transportModes: [{ mode: WALK }]
      ) {
        itineraries {
          duration
          legs {
            mode
            startTime
            endTime
            headsign
            from { name lat lon }
            to { name lat lon }
            route { shortName longName }
            legGeometry { points }
            intermediateStops { name lat lon }
          }
        }
      }
    }
    """
    try:
        response = requests.post(
            OTP_URL,
            json={
                "query": query,
                "variables": {
                    "fromLat": from_lat, "fromLon": from_lon,
                    "toLat": station["lat"], "toLon": station["lon"],
                    "date": departure_date, "time": departure_time,
                },
            },
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
        response.raise_for_status()
        itinerary = ((response.json().get("data", {}).get("plan") or {}).get("itineraries") or [None])[0]
        if not itinerary or not itinerary.get("legs"):
            return None
        for leg in itinerary["legs"]:
            leg["isNearestStationAccess"] = True
            leg["usesStreetAccess"] = True
        return itinerary
    except (requests.exceptions.RequestException, IndexError, TypeError, ValueError):
        return None


def _terminal_station(trip):
    """Return the destination terminal from a GTFS trip headsign."""
    headsign = (trip.get("trip_headsign") or "").strip()
    if " to " in headsign.lower():
        return headsign.rsplit(" to ", 1)[-1].strip()
    return headsign


def _route_label(route):
    """Return the passenger-facing route number for an OTP route object."""
    for field in ("shortName", "longName"):
        value = (route.get(field) or "").strip()
        if value:
            return value
    return None


@lru_cache(maxsize=1)
def _trip_stop_sequences():
    sequences = {}
    for filename in REALTIME_FEEDS:
        with zipfile.ZipFile(DATA_DIRECTORY / filename, "r") as archive:
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
                sequences.setdefault(row["trip_id"], {
                    "route": route_name,
                    "direction": _terminal_station(trip),
                    "is_bus": filename != "gtfs-rail.zip",
                    "stops": [],
                })["stops"].append((row["stop_id"], float(stop["stop_lat"]), float(stop["stop_lon"])))
    return sequences


@lru_cache(maxsize=1)
def _station_transfer_links():
    """Load explicit same-station transfer links from the Rapid Rail GTFS."""
    with zipfile.ZipFile(DATA_DIRECTORY / "gtfs-rail.zip") as archive:
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


def _apply_station_transfer_times(itineraries):
    """Replace OTP street detours with audited interchange transfer times."""
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


@lru_cache(maxsize=1)
def _bus_stop_locations():
    """Return the static locations of all Rapid Bus and MRT feeder stops."""
    locations = []
    for filename in ("gtfs-bus.zip", "gtfs-mrtfeeder.zip"):
        with zipfile.ZipFile(DATA_DIRECTORY / filename, "r") as archive:
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
    return locations


@lru_cache(maxsize=1)
def _transit_stop_locations():
    """Return every GTFS stop for matching itinerary places to station IDs."""
    locations = []
    for filename in ("gtfs-bus.zip", "gtfs-mrtfeeder.zip", "gtfs-rail.zip", "gtfs-ktmb.zip"):
        with zipfile.ZipFile(DATA_DIRECTORY / filename, "r") as archive:
            with archive.open("stops.txt") as file:
                for row in csv.DictReader(io.TextIOWrapper(file, encoding="utf-8-sig")):
                    try:
                        locations.append((
                            row["stop_id"], row.get("stop_name", "Station"),
                            float(row["stop_lat"]), float(row["stop_lon"]),
                        ))
                    except (KeyError, TypeError, ValueError):
                        continue
    return locations


def _station_id_at_place(place):
    """Resolve OTP's street-linked stop location to a GTFS station ID."""
    try:
        latitude, longitude = float(place["lat"]), float(place["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    stop_id, name, stop_lat, stop_lon = min(
        _transit_stop_locations(),
        key=lambda stop: _distance_meters(latitude, longitude, stop[2], stop[3]),
    )
    if _distance_meters(latitude, longitude, stop_lat, stop_lon) > 180:
        return None
    return {"id": stop_id, "name": name}


def _recent_station_presence(stop_ids):
    """Return anonymous recent-presence counts, never individual locations."""
    if not stop_ids or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return {}
    cutoff = (datetime.now().astimezone() - timedelta(
        minutes=STATION_PRESENCE_WINDOW_MINUTES
    )).isoformat()
    try:
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/{SUPABASE_PRESENCE_TABLE}",
            params={
                "select": "station_id",
                "station_id": f"in.({','.join(stop_ids)})",
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
            if row.get("station_id") in stop_ids:
                counts[row["station_id"]] += 1
        return dict(counts)
    except (requests.exceptions.RequestException, TypeError, ValueError):
        return {}


def _station_activity_level(reports):
    if reports >= 10:
        return "high"
    if reports >= 3:
        return "moderate"
    return "low"


def _attach_congestion_metadata(itineraries):
    """Attach Supabase-only station/stop congestion signals to each route."""
    station_matches = {}
    for itinerary in itineraries:
        for leg in itinerary.get("legs") or []:
            if (leg.get("mode") or "").upper() in {"WALK", "HAIL", ""}:
                continue
            for place in (leg.get("from") or {}, leg.get("to") or {}):
                station = _station_id_at_place(place)
                if station is not None:
                    station_matches[station["id"]] = station
    presence = _recent_station_presence(list(station_matches))

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
        # Congestion ranking deliberately uses only anonymous Supabase reports
        # for the stops/stations on this itinerary. TomTom remains an ETA
        # input for vehicles, never a passenger-congestion classification.
        itinerary["congestionScore"] = station_penalty * 20
        itinerary["congestion"] = {
            "stationActivity": stations,
            "stationSource": "anonymous_presence" if presence else "unavailable",
        }


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
    for feed_name, vehicle in _realtime_cache["vehicles"]:
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
        speed = vehicle.position.speed if has_reported_speed else 6.0
        traffic_factor = None
        # Road traffic applies only to bus and MRT feeder vehicles, never rail.
        # Use it as a fallback when GTFS-RT has no measured vehicle speed.
        if not has_reported_speed and feed_name != "gtfs-rail.zip" and traffic_lookups_remaining:
            traffic_factor = _traffic_delay_factor(vehicle.position.latitude, vehicle.position.longitude)
            traffic_lookups_remaining -= 1
        eta_seconds = int((distance / speed) * (traffic_factor or 1.0))
        if eta_seconds <= 7200:
            estimated_time = datetime.now().astimezone() + timedelta(seconds=eta_seconds)
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
    """Match an OTP bus boarding point to its next GTFS-Realtime vehicle."""
    if (leg.get("mode") or "").upper() != "BUS" or not route_name:
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
    # OTP's boarding place should resolve to a real bus stop, rather than a
    # nearby street vertex or interchange entrance.
    if _distance_meters(latitude, longitude, stop_latitude, stop_longitude) > 150:
        return None

    normalized_route = str(route_name).strip().casefold()
    for estimate in _live_vehicle_estimates(stop_id):
        if (estimate["is_bus"] and
                str(estimate["route"]).strip().casefold() == normalized_route):
            return estimate
    return None


def _itinerary_category(itinerary):
    """Classify a choice by its dominant public-transport mode."""
    modes = [
        (leg.get("mode") or "").upper()
        for leg in itinerary.get("legs") or []
    ]
    if "HAIL" in modes:
        return "ehailing"
    rail_legs = sum(mode in {"RAIL", "SUBWAY", "TRAM"} for mode in modes)
    bus_legs = modes.count("BUS")
    if rail_legs and rail_legs >= bus_legs:
        return "rail"
    if bus_legs:
        return "bus"
    return "walking"


def _prefer_south_quay_brt_transfer(itineraries, origin_station):
    """Use the BRT at South Quay for the co-located USJ 7 LRT interchange.

    OTP currently does not chain its BRT and LRT platform vertices at USJ 7,
    despite their explicit GTFS transfer. Its fallback is a long walking leg
    along the same corridor. This policy substitutes the real BRT connection
    only when the requested journey is already walking from South Quay to the
    USJ 7 LRT platform to continue by rail.
    """
    if origin_station.get("id") != "BRT6":
        return
    brt_destination = {
        "name": "USJ7",
        "lat": 3.0548355,
        "lon": 101.591941,
    }
    for itinerary in itineraries:
        legs = itinerary.get("legs") or []
        # OTP sometimes already supplies the BRT leg. In that case do not add
        # the South Quay safeguard a second time.
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
                end_time = int(leg["endTime"])
            except (KeyError, TypeError, ValueError):
                continue
            if not reaches_usj7:
                continue
            brt_duration = 120
            legs[index] = {
                "mode": "TRAM",
                "startTime": str(start_time),
                "endTime": str(start_time + brt_duration * 1000),
                "headsign": "USJ7",
                "from": {
                    "name": origin_station["name"],
                    "lat": origin_station["lat"],
                    "lon": origin_station["lon"],
                },
                "to": brt_destination,
                "route": {"shortName": "BRT"},
                "intermediateStops": [],
            }
            itinerary["duration"] = max(
                0, itinerary.get("duration", 0) - max(0, (end_time - start_time) // 1000 - brt_duration)
            )
            break


@app.get("/api/transit/stops/{stop_id}/departures")
def get_next_departures(stop_id: str, limit: int | None = None):
    now = datetime.now().astimezone()
    next_departures = _live_vehicle_estimates(stop_id)

    for route, scheduled_time, active_days in _scheduled_departures_by_stop().get(stop_id, []):
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
        # Keep the soonest arrival for every bus route, and for each direction
        # of rail/BRT. GTFS schedules can contain hundreds of future trips for
        # one line, while the station UI needs distinct services only.
        key = (departure["route"],) if departure["is_bus"] else (
            departure["route"], departure["direction"]
        )
        if key not in seen_departures:
            unique_departures.append(departure)
            seen_departures.add(key)

    # Return every available arrival by default. A client may still request a
    # positive limit when it needs a compact result.
    if limit is not None:
        limit = max(1, limit)
        unique_departures = unique_departures[:limit]
    return {"departures": unique_departures}

@app.get("/api/transit/stops")
def get_gtfs_stops():
    # Array mapping our split official feed zip layers
    gtfs_files = [
        "./gtfs-bus.zip",
        "./gtfs-mrtfeeder.zip",
        "./gtfs-rail.zip",
        "./gtfs-ktmb.zip",
    ]
    features = []
    
    for zip_path in gtfs_files:
        try:
            with zipfile.ZipFile(zip_path, 'r') as z:
                if 'stops.txt' not in z.namelist():
                    continue # Skip safely if a file is malformed
                    
                # Identify if this specific zip represents the rail network
                is_rail_dataset = "rail" in zip_path.lower()
                
                with z.open('stops.txt') as f:
                    text_stream = io.TextIOWrapper(f, encoding='utf-8-sig')
                    reader = csv.DictReader(text_stream)
                    
                    for row in reader:
                        try:
                            stop_lat = float(row['stop_lat'])
                            stop_lon = float(row['stop_lon'])
                            stop_name = row.get('stop_name', 'Unknown Stop')
                            stop_id = row.get('stop_id', '')
                            
                            feature = {
                                "type": "Feature",
                                "geometry": {
                                "type": "Point",
                                "coordinates": [stop_lon, stop_lat]
                                },
                                "properties": {
                                    "id": stop_id,
                                    "name": stop_name,
                                    # Tag dynamically so Flutter can apply distinct styles
                                    "transit_type": "rail" if is_rail_dataset else "bus"
                                }
                            }
                            features.append(feature)
                        except (ValueError, KeyError):
                            continue
                            
        except FileNotFoundError:
            # Continue to next file if one is missing during initial testing
            continue
            
    if not features:
        raise HTTPException(status_code=500, detail="No GTFS data source packages found.")
        
    return {
        "type": "FeatureCollection",
        "features": features
    }

@app.post("/api/route")
def get_transit_route(request: RouteRequest):
    departure = datetime.now().astimezone()
    departure_date = request.departure_date or departure.strftime("%Y-%m-%d")
    departure_time = request.departure_time or departure.strftime("%H:%M:%S")
    try:
        requested_departure = datetime.strptime(
            f"{departure_date} {departure_time}", "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=departure.tzinfo)
    except ValueError:
        requested_departure = departure
    nearest_station = _nearest_rail_or_brt_station(request.from_lat, request.from_lon)
    street_access = _street_walk_to_station(
        request.from_lat,
        request.from_lon,
        nearest_station,
        departure_date,
        departure_time,
    )
    if street_access is None:
        itinerary = _fallback_itinerary(
            request,
            "HAIL",
            "No walkable access to the nearest station was found; showing an e-hailing estimate.",
        )
        itinerary["routeCategory"] = "ehailing"
        return {"itineraries": [itinerary]}
    access_walk_seconds = int((street_access or {}).get("duration") or 0)
    station_departure = requested_departure + timedelta(seconds=access_walk_seconds)
    bus_mode = "" if request.prefer_brt else "          { mode: BUS },\n"

    # Constructing the strict OTP v2 GraphQL itinerary query
    query = """
    query GetTransitRoute($fromLat: Float!, $fromLon: Float!, $toLat: Float!, $toLon: Float!, $date: String!, $time: String!) {
      plan(
        from: { lat: $fromLat, lon: $fromLon }
        to: { lat: $toLat, lon: $toLon }
        date: $date
        time: $time
        numItineraries: 8
        # Allow a short window for the next service. This lets the route join
        # a nearby station rather than abandoning it for a long walk just
        # because a frequent BRT or rail vehicle is a few minutes away.
        searchWindow: 1800
        # Prefer pedestrian paths and lower-risk walking links for transfers
        # before using ordinary roadside segments. OTP falls back to roads only
        # where the OSM pedestrian network does not provide a connection.
        optimize: SAFE
        # Once the access walk reaches the nearest station, make boarding the
        # available rail/BRT service preferable to walking along the corridor
        # to a farther station (for example South Quay to USJ 7).
        walkReluctance: 4.0
        transportModes: [
__BUS_MODE__
          { mode: TRAM },
          { mode: RAIL }, 
          { mode: SUBWAY }, 
          { mode: WALK }
        ]
      ) {
        itineraries {
          duration
          legs {
            mode
            startTime
            endTime
            headsign
            from {
              name
              lat
              lon
            }
            to {
              name
              lat
              lon
            }
            route {
              shortName
              longName
            }
            legGeometry {
              points
            }
            intermediateStops {
              name
              lat
              lon
            }
          }
        }
      }
    }
    """
    query = query.replace("__BUS_MODE__", bus_mode)
    
    variables = {
        "fromLat": nearest_station["lat"],
        "fromLon": nearest_station["lon"],
        "toLat": request.to_lat,
        "toLon": request.to_lon,
        "date": station_departure.strftime("%Y-%m-%d"),
        "time": station_departure.strftime("%H:%M:%S"),
    }
    
    try:
        response = requests.post(
            OTP_URL, 
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json"},
            timeout=60
        )
        
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail="Invalid response from OpenTripPlanner engine.")
            
        data = response.json()
        
        # Check for upstream GraphQL errors
        if "errors" in data:
            raise HTTPException(status_code=400, detail=data["errors"][0]["message"])
            
        plan_data = data.get("data", {}).get("plan") or {}
        itineraries = plan_data.get("itineraries") or []

        if access_walk_seconds > 25 and street_access is not None:
            access_walk_legs = street_access["legs"]
            for itinerary in itineraries:
                legs = itinerary.get("legs") or []
                if any(leg.get("mode", "").upper() not in {"", "WALK"} for leg in legs):
                    itinerary["legs"] = [dict(leg) for leg in access_walk_legs] + legs
                    itinerary["duration"] = itinerary.get("duration", 0) + access_walk_seconds

        _prefer_south_quay_brt_transfer(itineraries, nearest_station)
        _apply_station_transfer_times(itineraries)

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
            itinerary["isRecommended"] = any(
                (leg.get("route") or {}).get("shortName", "").upper() == "BRT"
                for leg in itinerary.get("legs") or []
            )

        _attach_congestion_metadata(itineraries)

        # A covered walkway is preferred to an exposed one even when it is
        # longer. Journey duration and BRT use only break ties with the same
        # amount of uncovered walking.
        itineraries.sort(
            key=lambda itinerary: (
                itinerary["uncoveredWalkSeconds"],
                itinerary.get("congestionScore", 0),
                -int(itinerary["isRecommended"]),
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
        # Keep route choices meaningfully different: one rail-dominant option,
        # one bus-dominant option, and e-hailing. OTP often returns the same
        # BRT route repeatedly with only a different wait time.
        category_choices = {}
        fallback_itineraries = []
        for itinerary in unique_itineraries:
            category = _itinerary_category(itinerary)
            itinerary["routeCategory"] = category
            if category in {"rail", "bus"} and category not in category_choices:
                category_choices[category] = itinerary
            else:
                fallback_itineraries.append(itinerary)
        itineraries = [
            category_choices[category]
            for category in ("rail", "bus")
            if category in category_choices
        ]
        if not itineraries and fallback_itineraries:
            itineraries = [fallback_itineraries[0]]

        has_transit = any(
            leg.get("mode", "").upper() not in {"WALK", ""}
            for itinerary in itineraries
            for leg in itinerary.get("legs") or []
        )

        if has_transit:
            # Keep a direct e-hailing estimate available as a user-selectable
            # alternative whenever OTP finds public-transport itineraries.
            itineraries.append(_fallback_itinerary(
                request,
                "HAIL",
                "Approximate direct e-hailing journey.",
            ))
        else:
            if itineraries:
                # OTP found a walkable route but no public-transport service.
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

        # Normalize OTP's nested GraphQL fields into the exact shape consumed by
        # Flutter's transit-leg list. Intermediate stops retain OTP's order.
        for itinerary in itineraries:
            itinerary["routeCategory"] = _itinerary_category(itinerary)
            for leg in itinerary.get("legs") or []:
                route_info = leg.get("route") or {}
                route_label = _route_label(route_info)
                leg["routeShortName"] = route_label
                leg["headsign"] = _terminal_station({
                    "trip_headsign": leg.get("headsign")
                })
                leg["isSheltered"] = bool(leg.get("isSheltered"))

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
        raise HTTPException(status_code=503, detail=f"Could not connect to OTP engine: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    # FastAPI will listen natively on port 8000
    uvicorn.run(app, host="0.0.0.0", port=8000)
