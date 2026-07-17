import json
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws
from skyfield.api import EarthSatellite, load

from api import handler as api_handler
from shared.passes import azimuth_to_compass, compute_passes, subpoint_of

ISS_TLE = (
    "ISS (ZARYA)",
    "1 25544U 98067A   26191.50000000  .00016717  00000-0  10270-3 0  9008",
    "2 25544  51.6423 339.8700 0007417  17.6667  85.6479 15.50423408123456",
)

# Fixed evaluation time near the TLE epoch keeps results deterministic.
FIXED_TIME = datetime(2026, 7, 16, 0, 0, 0, tzinfo=timezone.utc)

ts = load.timescale()


@pytest.fixture(autouse=True)
def fresh_module_caches(monkeypatch):
    monkeypatch.setattr(api_handler, "_ts", None)
    monkeypatch.setattr(api_handler, "_eph", None)
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
            }
        )
        yield table


def _iss():
    name, line1, line2 = ISS_TLE
    return EarthSatellite(line1, line2, name, ts)


def test_subpoint_is_plausible_for_iss():
    point = subpoint_of(_iss(), ts.from_datetime(FIXED_TIME))

    # ISS inclination is 51.64 deg — the ground track can't leave that band —
    # and its orbit altitude sits around 350-450 km.
    assert -52 <= point["lat"] <= 52
    assert -180 <= point["lon"] <= 180
    assert 300 <= point["alt_km"] <= 500


def test_compute_passes_returns_complete_ordered_passes():
    passes = compute_passes(
        _iss(),
        observer_lat=26.0,
        observer_lon=-80.0,
        ts=ts,
        eph=None,
        start=FIXED_TIME,
        hours=24,
        min_elevation_deg=10,
    )

    assert passes, "ISS should pass over Florida at least once in 24h"
    for p in passes:
        assert p["rise"] < p["culminate"] < p["set"]
        assert p["max_elevation_deg"] >= 10
        assert p["direction"] in azimuth_to_compass.__globals__["_COMPASS_POINTS"]
        assert p["visible"] is None  # no ephemeris supplied


def test_azimuth_to_compass_wraps():
    assert azimuth_to_compass(0) == "N"
    assert azimuth_to_compass(359) == "N"
    assert azimuth_to_compass(45) == "NE"
    assert azimuth_to_compass(180) == "S"


def test_list_satellites(catalog_table):
    response = api_handler.handler({"routeKey": "GET /satellites"}, None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["satellites"] == [
        {
            "id": "25544",
            "name": "ISS (ZARYA)",
            "tle_fetched_at": "2026-07-10T12:00:00+00:00",
        }
    ]


def test_position_route_returns_live_subpoint(catalog_table):
    response = api_handler.handler(
        {
            "routeKey": "GET /satellites/{id}/position",
            "pathParameters": {"id": "25544"},
        },
        None,
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["name"] == "ISS (ZARYA)"
    assert -52 <= body["lat"] <= 52


def test_all_positions_route(catalog_table):
    response = api_handler.handler({"routeKey": "GET /positions"}, None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert len(body["positions"]) == 1
    assert body["positions"][0]["id"] == "25544"


def test_unknown_satellite_404(catalog_table):
    response = api_handler.handler(
        {
            "routeKey": "GET /satellites/{id}/position",
            "pathParameters": {"id": "99999"},
        },
        None,
    )

    assert response["statusCode"] == 404


def test_passes_route_requires_lat_lon(catalog_table):
    response = api_handler.handler(
        {
            "routeKey": "GET /satellites/{id}/passes",
            "pathParameters": {"id": "25544"},
            "queryStringParameters": {"lat": "26.0"},
        },
        None,
    )

    assert response["statusCode"] == 400
    assert "lon" in json.loads(response["body"])["error"]


def test_passes_route_rejects_out_of_range_observer(catalog_table):
    response = api_handler.handler(
        {
            "routeKey": "GET /satellites/{id}/passes",
            "pathParameters": {"id": "25544"},
            "queryStringParameters": {"lat": "91", "lon": "0"},
        },
        None,
    )

    assert response["statusCode"] == 400


def test_unknown_route_404():
    response = api_handler.handler({"routeKey": "DELETE /nope"}, None)

    assert response["statusCode"] == 404


def test_visible_passes_are_json_serializable(tmp_path):
    """Run the real visibility branch (sunlit + darkness) against the same
    ephemeris the Lambda uses, and require the result to survive json.dumps —
    numpy types leaking into the response is exactly the bug this caught."""
    import zipfile
    from pathlib import Path

    from skyfield.iokit import load_file

    layer_zip = (
        Path(__file__).parent.parent
        / "src" / "layers" / "skyfield" / "dist" / "skyfield-layer.zip"
    )
    if not layer_zip.exists():
        pytest.skip("layer zip not built; run src/layers/skyfield/build.py")

    with zipfile.ZipFile(layer_zip) as zf:
        zf.extract("data/de421.bsp", tmp_path)
    eph = load_file(tmp_path / "data" / "de421.bsp")

    passes = compute_passes(
        _iss(),
        observer_lat=26.0,
        observer_lon=-80.0,
        ts=ts,
        eph=eph,
        start=FIXED_TIME,
        hours=48,
        min_elevation_deg=10,
    )

    assert passes
    json.dumps(passes)  # must not raise
    assert all(isinstance(p["visible"], bool) for p in passes)
