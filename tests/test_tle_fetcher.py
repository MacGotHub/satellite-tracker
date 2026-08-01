import json
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from src.tle_fetch.handler import (
    archive_raw_tle,
    handler,
    parse_tle,
    write_satellites,
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
    write_satellites(table, satellites, "2026-07-10T12:00:00+00:00", "stations")

    item = table.get_item(Key={"pk": "25544", "sk": "TLE"})["Item"]
    assert item["name"] == "ISS (ZARYA)"
    assert item["line1"] == satellites[0]["line1"]
    assert item["group"] == "stations"


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
    ):
        response = handler({}, None)

    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["satellite_count"] == 2
    assert body["groups"] == ["stations"]
    assert body["per_group"] == {"stations": 2}


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

    with patch("src.tle_fetch.handler.fetch_tle_text", side_effect=fake_fetch):
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
