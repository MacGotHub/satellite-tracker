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
    write_satellites(table, satellites, "2026-07-10T12:00:00+00:00")

    item = table.get_item(Key={"pk": "25544", "sk": "TLE"})["Item"]
    assert item["name"] == "ISS (ZARYA)"
    assert item["line1"] == satellites[0]["line1"]


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
    assert body["group"] == "stations"


def test_handler_raises_on_empty_tle_response(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "test-table")
    monkeypatch.setenv("BUCKET_NAME", "test-bucket")

    with patch("src.tle_fetch.handler.fetch_tle_text", return_value=""):
        with pytest.raises(ValueError):
            handler({}, None)
