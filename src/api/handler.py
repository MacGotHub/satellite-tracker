"""Position/pass API Lambda — the read side of the satellite tracker.

Serves the one HTTP API route the frontend still calls: the raw TLE catalog
that the Phase 1 pipeline keeps fresh in DynamoDB. `GET /positions`,
`GET /satellites/{id}/position`, and `GET /satellites/{id}/passes` were
retired in Phase 6 (Steps 3 and 5): the frontend moved position/pass math to
client-side satellite.js in Steps 1-2, leaving all three with no consumers.
Skyfield/numpy/the de421 ephemeris are gone from this Lambda along with
them — that math still runs server-side, but only in the alerts Lambda
(src/alerts/handler.py + shared/passes.py) against the fixed home observer.
"""

import json
import os

import boto3

_table = None

# Optional SATCAT enrichment attributes (src/tle_fetch/handler.py) — only
# present once a satellite's group has had at least one successful SATCAT
# fetch, so these ride along when present rather than padding every
# response with nulls for satellites that don't have them yet.
_OPTIONAL_SATCAT_FIELDS = ("object_type", "owner", "launch_date", "decay_date")


def _catalog_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def _scan_catalog() -> list:
    # The catalog is a couple dozen items; a Scan is the right tool here.
    # Revisit only if the watchlist ever grows past a single page (1 MB).
    #
    # Since Phase 4, this table also holds alert-dedupe items
    # (sk = ALERT#<rise> / DIGEST#<rise>) alongside each satellite's TLE
    # item — filter to TLE records only, or those get scanned too and
    # blow up the response with items missing line1/line2.
    items = []
    kwargs = {
        "FilterExpression": "sk = :sk",
        "ExpressionAttributeValues": {":sk": "TLE"},
    }
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


def _serialize_satellite(item: dict) -> dict:
    satellite = {
        "id": item["pk"],
        "name": item["name"],
        "tle_fetched_at": item["fetched_at"],
        "line1": item["line1"],
        "line2": item["line2"],
        # Default covers items written before Phase 6 Step 3 tagged
        # them by source group — self-heals on their next 2h refresh.
        "group": item.get("group", "stations"),
    }
    for field in _OPTIONAL_SATCAT_FIELDS:
        if field in item:
            satellite[field] = item[field]
    if "rcs" in item:
        # DynamoDB returns Number attributes as Decimal — json.dumps()
        # raises on that type directly, so cast explicitly rather than
        # reaching for a custom encoder for one field.
        satellite["rcs"] = float(item["rcs"])
    return satellite


def _list_satellites(event) -> dict:
    satellites = [_serialize_satellite(item) for item in _scan_catalog()]
    satellites.sort(key=lambda s: s["name"])
    return _response(200, {"satellites": satellites})


ROUTES = {
    "GET /satellites": _list_satellites,
}


def handler(event, context):
    route = ROUTES.get(event.get("routeKey", ""))
    if route is None:
        return _response(404, {"error": "unknown route"})
    return route(event)
