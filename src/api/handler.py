"""Position/pass API Lambda — the read side of the satellite tracker.

Serves four HTTP API routes from the TLE catalog that the Phase 1 pipeline
keeps fresh in DynamoDB. Positions are propagated from the stored TLE at
request time rather than precomputed — an exact answer for "now" is cheap,
and only the TLE itself needs refreshing (on the Phase 1 schedule).

Skyfield, numpy, and the de421 ephemeris arrive via the Lambda layer
(/opt/python and /opt/data); only this handler and shared/ live in the
function zip.
"""

import json
import os

import boto3
from skyfield.api import EarthSatellite, load
from skyfield.iokit import load_file

from shared.passes import compute_passes, subpoint_of

# Module-level caches: Lambda reuses the execution environment between
# invocations, and the timescale/ephemeris/table handles are the expensive
# part of a request. Lazy (not import-time) so unit tests can stub first.
_ts = None
_eph = None
_table = None


def _timescale():
    global _ts
    if _ts is None:
        _ts = load.timescale()
    return _ts


def _ephemeris():
    global _eph
    if _eph is None:
        _eph = load_file(os.environ.get("EPHEMERIS_PATH", "/opt/data/de421.bsp"))
    return _eph


def _catalog_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def _satellite_from_item(item) -> EarthSatellite:
    return EarthSatellite(
        item["line1"], item["line2"], item["name"], _timescale()
    )


def _get_item(norad_id: str):
    return _catalog_table().get_item(
        Key={"pk": norad_id, "sk": "TLE"}
    ).get("Item")


def _scan_catalog() -> list:
    # The catalog is a couple dozen items; a Scan is the right tool here.
    # Revisit only if the watchlist ever grows past a single page (1 MB).
    items = []
    kwargs = {}
    while True:
        page = _catalog_table().scan(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _response(status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _list_satellites(event) -> dict:
    satellites = [
        {"id": item["pk"], "name": item["name"], "tle_fetched_at": item["fetched_at"]}
        for item in _scan_catalog()
    ]
    satellites.sort(key=lambda s: s["name"])
    return _response(200, {"satellites": satellites})


def _one_position(event) -> dict:
    norad_id = event["pathParameters"]["id"]
    item = _get_item(norad_id)
    if item is None:
        return _response(404, {"error": f"satellite {norad_id} not in catalog"})
    satellite = _satellite_from_item(item)
    now = _timescale().now()
    return _response(
        200,
        {
            "id": item["pk"],
            "name": item["name"],
            "time": now.utc_iso(),
            **subpoint_of(satellite, now),
        },
    )


def _all_positions(event) -> dict:
    now = _timescale().now()
    positions = []
    for item in _scan_catalog():
        satellite = _satellite_from_item(item)
        positions.append(
            {"id": item["pk"], "name": item["name"], **subpoint_of(satellite, now)}
        )
    positions.sort(key=lambda p: p["name"])
    return _response(200, {"time": now.utc_iso(), "positions": positions})


def _passes(event) -> dict:
    norad_id = event["pathParameters"]["id"]
    params = event.get("queryStringParameters") or {}
    try:
        lat = float(params["lat"])
        lon = float(params["lon"])
        hours = float(params.get("hours", 24))
        min_elevation = float(params.get("min_elevation", 10))
    except KeyError as missing:
        return _response(400, {"error": f"missing required query param {missing}"})
    except ValueError:
        return _response(400, {"error": "lat/lon/hours/min_elevation must be numbers"})
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return _response(400, {"error": "lat must be in [-90, 90], lon in [-180, 180]"})
    if not (0 < hours <= 72):
        return _response(400, {"error": "hours must be in (0, 72]"})

    item = _get_item(norad_id)
    if item is None:
        return _response(404, {"error": f"satellite {norad_id} not in catalog"})

    satellite = _satellite_from_item(item)
    passes = compute_passes(
        satellite,
        observer_lat=lat,
        observer_lon=lon,
        ts=_timescale(),
        eph=_ephemeris(),
        hours=hours,
        min_elevation_deg=min_elevation,
    )
    return _response(
        200,
        {
            "id": item["pk"],
            "name": item["name"],
            "observer": {"lat": lat, "lon": lon},
            "hours": hours,
            "min_elevation_deg": min_elevation,
            "passes": passes,
        },
    )


ROUTES = {
    "GET /satellites": _list_satellites,
    "GET /satellites/{id}/position": _one_position,
    "GET /positions": _all_positions,
    "GET /satellites/{id}/passes": _passes,
}


def handler(event, context):
    route = ROUTES.get(event.get("routeKey", ""))
    if route is None:
        return _response(404, {"error": "unknown route"})
    return route(event)
