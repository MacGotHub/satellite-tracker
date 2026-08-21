import json

import boto3
import pytest
from moto import mock_aws

from api import handler as api_handler

ISS_TLE = (
    "ISS (ZARYA)",
    "1 25544U 98067A   26191.50000000  .00016717  00000-0  10270-3 0  9008",
    "2 25544  51.6423 339.8700 0007417  17.6667  85.6479 15.50423408123456",
)


@pytest.fixture(autouse=True)
def fresh_module_caches(monkeypatch):
    monkeypatch.setattr(api_handler, "_table", None)


@pytest.fixture
def catalog_table(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "test-table")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="test-table",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        name, line1, line2 = ISS_TLE
        table.put_item(
            Item={
                "pk": "25544",
                "sk": "TLE",
                "name": name,
                "line1": line1,
                "line2": line2,
                "fetched_at": "2026-07-10T12:00:00+00:00",
                "group": "stations",
            }
        )
        # Phase 4's alerts Lambda writes dedupe items into this same table
        # (sk = ALERT#<rise> / DIGEST#<rise>); the catalog scan must ignore
        # them rather than trip over their missing line1/line2/name/fetched_at.
        table.put_item(Item={"pk": "25544", "sk": "ALERT#2026-07-30T01:07:36Z"})
        table.put_item(Item={"pk": "25544", "sk": "DIGEST#2026-07-30T01:07:35Z"})
        yield table


def test_list_satellites(catalog_table):
    response = api_handler.handler({"routeKey": "GET /satellites"}, None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["satellites"] == [
        {
            "id": "25544",
            "name": "ISS (ZARYA)",
            "tle_fetched_at": "2026-07-10T12:00:00+00:00",
            "line1": ISS_TLE[1],
            "line2": ISS_TLE[2],
            "group": "stations",
        }
    ]


def test_list_satellites_includes_satcat_fields_when_present(catalog_table):
    from decimal import Decimal

    catalog_table.put_item(
        Item={
            "pk": "48274",
            "sk": "TLE",
            "name": "CSS (TIANHE)",
            "line1": "1 48274U 21035A   26191.50000000  .00025000  00000-0  25000-3 0  9005",
            "line2": "2 48274  41.4750  10.0000 0001000 100.0000 260.0000 15.60000000123456",
            "fetched_at": "2026-07-10T12:00:00+00:00",
            "group": "stations",
            "object_type": "Payload",
            "owner": "People's Republic of China",
            "launch_date": "2021-04-29",
            "rcs": Decimal("10.5"),
        }
    )

    response = api_handler.handler({"routeKey": "GET /satellites"}, None)

    body = json.loads(response["body"])
    tianhe = next(s for s in body["satellites"] if s["id"] == "48274")
    assert tianhe["object_type"] == "Payload"
    assert tianhe["owner"] == "People's Republic of China"
    assert tianhe["launch_date"] == "2021-04-29"
    assert tianhe["rcs"] == 10.5
    assert "decay_date" not in tianhe  # never set on this item, stays absent

    # The ISS fixture item has no SATCAT fields at all (pre-enrichment
    # shape) — must serialize cleanly without them, not KeyError or null-pad.
    iss = next(s for s in body["satellites"] if s["id"] == "25544")
    assert "object_type" not in iss
    assert "rcs" not in iss


def test_list_satellites_defaults_group_for_untagged_items(catalog_table):
    # Items written before Phase 6 Step 3 added group-tagging won't have
    # the attribute until their next 2h refresh — must not KeyError.
    catalog_table.put_item(
        Item={
            "pk": "48274",
            "sk": "TLE",
            "name": "CSS (TIANHE)",
            "line1": "1 48274U 21035A   26191.50000000  .00025000  00000-0  25000-3 0  9005",
            "line2": "2 48274  41.4750  10.0000 0001000 100.0000 260.0000 15.60000000123456",
            "fetched_at": "2026-07-10T12:00:00+00:00",
        }
    )

    response = api_handler.handler({"routeKey": "GET /satellites"}, None)

    body = json.loads(response["body"])
    untagged = next(s for s in body["satellites"] if s["id"] == "48274")
    assert untagged["group"] == "stations"


def test_positions_route_retired():
    # GET /positions was retired in Phase 6 Step 3 — no consumers left,
    # and it would have Skyfield-propagated the whole catalog per request.
    response = api_handler.handler({"routeKey": "GET /positions"}, None)

    assert response["statusCode"] == 404


def test_position_route_retired():
    # GET /satellites/{id}/position was retired in Phase 6 Step 5 — the
    # frontend moved to client-side satellite.js propagation in Steps 1-2
    # and had no consumers left for this route either.
    response = api_handler.handler(
        {
            "routeKey": "GET /satellites/{id}/position",
            "pathParameters": {"id": "25544"},
        },
        None,
    )

    assert response["statusCode"] == 404


def test_passes_route_retired():
    # GET /satellites/{id}/passes was retired alongside it in Step 5 — the
    # frontend computes passes locally via findPassesLocal() in app.js.
    response = api_handler.handler(
        {
            "routeKey": "GET /satellites/{id}/passes",
            "pathParameters": {"id": "25544"},
            "queryStringParameters": {"lat": "26.0", "lon": "-80.0"},
        },
        None,
    )

    assert response["statusCode"] == 404


def test_unknown_route_404():
    response = api_handler.handler({"routeKey": "DELETE /nope"}, None)

    assert response["statusCode"] == 404
