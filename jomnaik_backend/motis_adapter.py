"""MOTIS REST adapter.

MOTIS returns ISO-8601 timestamps and its own leg schema.  The Flutter app
already consumes the middleware's normalized itinerary shape, so this module
keeps the engine-specific translation in one place.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any

import requests


MOTIS_URL = os.getenv("MOTIS_URL", "http://localhost:8080").rstrip("/")
MOTIS_PLAN_PATH = "/api/v6/plan"


def _timestamp_ms(value: Any) -> str:
    if isinstance(value, (int, float)):
        # MOTIS uses ISO strings for plan legs, but accept epoch values for
        # compatibility with older deployments.
        number = float(value)
        return str(int(number * 1000 if number < 10_000_000_000 else number))
    text = str(value or "")
    if not text:
        return "0"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return str(int(parsed.timestamp() * 1000))
    except ValueError:
        return "0"


def _place(value: Any, fallback_name: str = "") -> dict[str, Any]:
    place = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {
        "name": place.get("name") or place.get("id") or fallback_name,
    }
    for target, source in (("lat", "lat"), ("lon", "lon")):
        if isinstance(place.get(source), (int, float)):
            result[target] = float(place[source])
    if place.get("id") is not None:
        result["id"] = str(place["id"])
    return result


def _mode(value: Any) -> str:
    mode = str(value or "").upper()
    if mode in {"FOOT", "WALK"}:
        return "WALK"
    if mode in {"CAR", "CAR_PARKING", "CAR_DROPOFF"}:
        return "HAIL"
    if mode in {"BUS", "COACH"}:
        return "BUS"
    if mode in {"TRAM", "SUBWAY", "RAIL", "SUBURBAN", "REGIONAL_RAIL"}:
        return mode
    return mode or "WALK"


def _leg(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    mode = _mode(source.get("mode"))
    route_short = source.get("routeShortName") or source.get("displayName")
    route = {
        "shortName": str(route_short) if route_short else "",
        "longName": str(source.get("routeLongName") or ""),
    }
    result: dict[str, Any] = {
        "mode": mode,
        "startTime": _timestamp_ms(source.get("startTime")),
        "endTime": _timestamp_ms(source.get("endTime")),
        "headsign": source.get("headsign") or "",
        "from": _place(source.get("from"), "Origin"),
        "to": _place(source.get("to"), "Destination"),
        "route": route,
        "intermediateStops": [
            _place(stop)
            for stop in (source.get("intermediateStops") or [])
            if isinstance(stop, dict)
        ],
        "realTime": bool(source.get("realTime", False)),
    }
    geometry = source.get("legGeometry")
    if isinstance(geometry, dict) and isinstance(geometry.get("points"), str):
        result["legGeometry"] = {"points": geometry["points"]}
    return result


def _itinerary(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    legs = [_leg(leg) for leg in (source.get("legs") or [])]
    duration = source.get("duration")
    try:
        duration_seconds = int(duration)
    except (TypeError, ValueError):
        duration_seconds = 0
    if not duration_seconds and legs:
        duration_seconds = max(
            0,
            (int(legs[-1]["endTime"]) - int(legs[0]["startTime"])) // 1000,
        )
    return {
        "duration": duration_seconds,
        "legs": legs,
        "transfers": source.get("transfers", 0),
        "motisItineraryId": source.get("id"),
    }


def plan(
    *,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    departure_time: datetime,
    from_stop_id: str | None = None,
    to_stop_id: str | None = None,
    timeout: float = 60,
) -> list[dict[str, Any]]:
    """Return normalized MOTIS itineraries for the middleware."""
    params = {
        "fromPlace": from_stop_id or f"{from_lat},{from_lon}",
        "toPlace": to_stop_id or f"{to_lat},{to_lon}",
        "time": departure_time.astimezone(timezone.utc).isoformat(),
        "searchWindow": 7200,
        "maxTransfers": 8,
        "maxItineraries": 8,
        "timetableView": "true",
        "detailedLegs": "true",
        "detailedTransfers": "true",
        "useRoutedTransfers": "true",
        "realtimeMode": "REALTIME",
        "transitModes": "TRANSIT",
        "preTransitModes": "WALK",
        "postTransitModes": "WALK",
        "directModes": "WALK",
        "withFares": "true",
    }
    response = requests.get(
        f"{MOTIS_URL}{MOTIS_PLAN_PATH}", params=params, timeout=timeout
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("MOTIS returned a non-object response")
    return [
        _itinerary(item)
        for item in (payload.get("itineraries") or [])
        if isinstance(item, dict)
    ]


def walk(
    *,
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    departure_time: datetime,
    timeout: float = 30,
) -> dict[str, Any] | None:
    """Return MOTIS's routed direct walking itinerary, if reachable."""
    params = {
        "fromPlace": f"{from_lat},{from_lon}",
        "toPlace": f"{to_lat},{to_lon}",
        "time": departure_time.astimezone(timezone.utc).isoformat(),
        "transitModes": "",
        "directModes": "WALK",
        "detailedLegs": "true",
        "maxDirectTime": 3600,
    }
    response = requests.get(
        f"{MOTIS_URL}{MOTIS_PLAN_PATH}", params=params, timeout=timeout
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return None
    direct = payload.get("direct") or []
    if not direct:
        return None
    return _itinerary(direct[0])
