import json
import time
import urllib.error
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from src.tle_fetch.handler import (
    TLE_TTL_SECONDS,
    archive_raw_tle,
    handler,
    parse_satcat,
    parse_tle,
    write_satellites,
)

# Every handler()-level test exercises the real per-group loop, which now
# always attempts a SATCAT fetch too — patch it to fail fast instead of
# hitting the network (or CelesTrak's rate policy) during a unit test.
NO_SATCAT = patch(
    "src.tle_fetch.handler.fetch_satcat_text",
    side_effect=urllib.error.URLError("no network in tests"),
)

SAMPLE_SATCAT_JSON = json.dumps(
    [
        {
            "OBJECT_NAME": "ISS (ZARYA)",
            "NORAD_CAT_ID": 25544,
            "OBJECT_TYPE": "PAY",
            "OWNER": "ISS",
            "LAUNCH_DATE": "1998-11-20",
            "DECAY_DATE": "",
            "RCS": 399.0524,
        },
        {
            "OBJECT_NAME": "SOME DEBRIS",
            "NORAD_CAT_ID": 694,
            "OBJECT_TYPE": "DEB",
            "OWNER": "US",
            "LAUNCH_DATE": "1975-01-01",
            "DECAY_DATE": "",
            "RCS": None,
        },
    ]
)

SAMPLE_TLE = """ISS (ZARYA)
1 25544U 98067A   26191.50000000  .00016717  00000-0  10270-3 0  9008
2 25544  51.6423 339.8700 0007417  17.6667  85.6479 15.50423408123456
CSS (TIANHE)
1 48274U 21035A   26191.50000000  .00025000  00000-0  25000-3 0  9005
2 48274  41.4750  10.0000 0001000 100.0000 260.0000 15.60000000123456
"""


def test_parse_tle_extracts_norad_id_and_lines():
    satellites = parse_tle(SAMPLE_TLE)

    assert len(satellites) == 2
    assert satellites[0]["norad_id"] == "25544"
    assert satellites[0]["name"] == "ISS (ZARYA)"
    assert satellites[1]["norad_id"] == "48274"


def test_parse_tle_ignores_trailing_blank_lines():
    satellites = parse_tle(SAMPLE_TLE + "\n\n")

    assert len(satellites) == 2


@mock_aws
def test_archive_raw_tle_writes_to_s3():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-bucket")

    key = archive_raw_tle(
        s3, "test-bucket", "stations", SAMPLE_TLE, "2026-07-10T12:00:00+00:00"
    )

    body = s3.get_object(Bucket="test-bucket", Key=key)["Body"].read().decode("utf-8")
    assert body == SAMPLE_TLE
    assert key == "raw/stations/2026-07-10T12:00:00+00:00.tle"


@mock_aws
def test_write_satellites_puts_items_in_dynamodb():
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
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

    satellites = parse_tle(SAMPLE_TLE)
    before = int(time.time())
    write_satellites(table, satellites, "2026-07-10T12:00:00+00:00", "stations")
    after = int(time.time())

    item = table.get_item(Key={"pk": "25544", "sk": "TLE"})["Item"]
    assert item["name"] == "ISS (ZARYA)"
    assert item["line1"] == satellites[0]["line1"]
    assert item["group"] == "stations"
    # expires_at refreshes on every fetch a satellite still appears in —
    # only satellites CelesTrak stops returning age out, per backlog item 1.
    assert before + TLE_TTL_SECONDS <= item["expires_at"] <= after + TLE_TTL_SECONDS


@mock_aws
def test_write_satellites_batches_past_25_items():
    # boto3's batch_writer() chunks into groups of 25 internally — this is
    # the one test that actually proves that chunking works end to end,
    # since every other fixture in this file stays under that boundary.
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="test-table-batch",
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

    satellites = [
        {
            "norad_id": str(10000 + i),
            "name": f"SYNTHETIC-{i}",
            "line1": f"1 {10000 + i}U 98067A   26191.50000000  .00016717  00000-0  10270-3 0  900{i % 10}",
            "line2": f"2 {10000 + i}  51.6423 339.8700 0007417  17.6667  85.6479 15.5042340812345{i % 10}",
        }
        for i in range(60)
    ]

    write_satellites(table, satellites, "2026-07-10T12:00:00+00:00", "starlink")

    scanned = table.scan()["Items"]
    assert len(scanned) == 60
    assert all(item["group"] == "starlink" for item in scanned)


@mock_aws
def test_handler_end_to_end(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "test-table")
    monkeypatch.setenv("BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("CELESTRAK_GROUP", "stations")

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-bucket")

    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
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

    with patch(
        "src.tle_fetch.handler.fetch_tle_text", return_value=SAMPLE_TLE
    ), NO_SATCAT:
        response = handler({}, None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["satellite_count"] == 2
    assert body["groups"] == ["stations"]
    assert body["per_group"] == {"stations": 2}
    # SATCAT fetch failed for the group — degrades to zero matches, not a
    # handler-level failure.
    assert body["satcat_matched"] == {"stations": 0}


OTHER_SAMPLE_TLE = """STARLINK-1007
1 44713U 19074A   26191.50000000  .00001000  00000-0  10000-3 0  9001
2 44713  53.0000 100.0000 0001000  90.0000 270.0000 15.06000000123456
"""


@mock_aws
def test_handler_fetches_and_tags_multiple_groups(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "test-table")
    monkeypatch.setenv("BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("CELESTRAK_GROUP", "stations, starlink")

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-bucket")

    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
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

    def fake_fetch(url):
        return OTHER_SAMPLE_TLE if "GROUP=starlink" in url else SAMPLE_TLE

    with patch("src.tle_fetch.handler.fetch_tle_text", side_effect=fake_fetch), NO_SATCAT:
        response = handler({}, None)

    body = json.loads(response["body"])
    assert body["groups"] == ["stations", "starlink"]
    assert body["per_group"] == {"stations": 2, "starlink": 1}
    assert body["satellite_count"] == 3
    assert set(body["archive_keys"].keys()) == {"stations", "starlink"}

    table = dynamodb.Table("test-table")
    stations_item = table.get_item(Key={"pk": "25544", "sk": "TLE"})["Item"]
    starlink_item = table.get_item(Key={"pk": "44713", "sk": "TLE"})["Item"]
    assert stations_item["group"] == "stations"
    assert starlink_item["group"] == "starlink"

    s3_keys = {obj["Key"] for obj in s3.list_objects_v2(Bucket="test-bucket")["Contents"]}
    assert s3_keys == {
        f"raw/stations/{body['fetched_at']}.tle",
        f"raw/starlink/{body['fetched_at']}.tle",
    }


def test_handler_raises_on_empty_tle_response(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "test-table")
    monkeypatch.setenv("BUCKET_NAME", "test-bucket")

    with patch("src.tle_fetch.handler.fetch_tle_text", return_value=""):
        with pytest.raises(ValueError):
            handler({}, None)


def test_parse_satcat_decodes_known_codes_and_pads_norad_id():
    satcat_by_id = parse_satcat(SAMPLE_SATCAT_JSON)

    # 694, not "00694", comes back from CelesTrak's JSON — parse_satcat has
    # to zero-pad to match parse_tle()'s zero-padded line1[2:7] keys.
    assert set(satcat_by_id.keys()) == {"25544", "00694"}
    assert satcat_by_id["25544"] == {
        "object_type": "Payload",
        "owner": "International Space Station",
        "launch_date": "1998-11-20",
        "decay_date": None,
        "rcs": 399.0524,
    }
    # RCS null in the source stays None rather than becoming 0 or "null".
    assert satcat_by_id["00694"]["rcs"] is None


def test_parse_satcat_falls_back_to_raw_code_for_unrecognized_values():
    raw = json.dumps(
        [
            {
                "OBJECT_NAME": "MYSTERY",
                "NORAD_CAT_ID": 1,
                "OBJECT_TYPE": "XYZ",
                "OWNER": "ZZZ",
                "LAUNCH_DATE": "",
                "DECAY_DATE": "",
                "RCS": None,
            }
        ]
    )

    satcat_by_id = parse_satcat(raw)

    # A code CelesTrak adds later that isn't in our table yet should show up
    # as-is rather than silently disappearing or raising.
    assert satcat_by_id["00001"]["object_type"] == "XYZ"
    assert satcat_by_id["00001"]["owner"] == "ZZZ"
    # Empty-string dates decode to None, same as a genuinely absent value.
    assert satcat_by_id["00001"]["launch_date"] is None


@mock_aws
def test_write_satellites_merges_satcat_fields_including_decimal_rcs():
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
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

    satellites = parse_tle(SAMPLE_TLE)  # ISS (ZARYA)=25544, CSS (TIANHE)=48274
    satcat_by_id = parse_satcat(SAMPLE_SATCAT_JSON)  # only covers 25544
    write_satellites(table, satellites, "2026-07-10T12:00:00+00:00", "stations", satcat_by_id)

    iss_item = table.get_item(Key={"pk": "25544", "sk": "TLE"})["Item"]
    assert iss_item["object_type"] == "Payload"
    assert iss_item["owner"] == "International Space Station"
    assert iss_item["launch_date"] == "1998-11-20"
    assert iss_item["rcs"] == Decimal("399.0524")
    assert "decay_date" not in iss_item  # empty string decoded to None, omitted

    # No SATCAT match for this one — TLE still writes, just without the
    # extra attributes, rather than failing the whole satellite.
    tianhe_item = table.get_item(Key={"pk": "48274", "sk": "TLE"})["Item"]
    assert "object_type" not in tianhe_item
    assert "rcs" not in tianhe_item


@mock_aws
def test_handler_merges_satcat_when_fetch_succeeds(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "test-table")
    monkeypatch.setenv("BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("CELESTRAK_GROUP", "stations")

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-bucket")

    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
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

    with patch(
        "src.tle_fetch.handler.fetch_tle_text", return_value=SAMPLE_TLE
    ), patch(
        "src.tle_fetch.handler.fetch_satcat_text", return_value=SAMPLE_SATCAT_JSON
    ):
        response = handler({}, None)

    body = json.loads(response["body"])
    assert body["satcat_matched"] == {"stations": 2}

    table = dynamodb.Table("test-table")
    iss_item = table.get_item(Key={"pk": "25544", "sk": "TLE"})["Item"]
    assert iss_item["owner"] == "International Space Station"
