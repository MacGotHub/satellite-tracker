import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from alerts import handler as alerts_handler

ISS_ITEM = {
    "pk": "25544",
    "sk": "TLE",
    "name": "ISS (ZARYA)",
    "line1": "1 25544U 98067A   26191.50000000  .00016717  00000-0  10270-3 0  9008",
    "line2": "2 25544  51.6423 339.8700 0007417  17.6667  85.6479 15.50423408123456",
    "fetched_at": "2026-07-10T12:00:00+00:00",
}


@pytest.fixture(autouse=True)
def alerts_env(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "test-table")
    monkeypatch.setenv("OBSERVER_PARAM", "/sattrack/observer")
    monkeypatch.setenv("WATCHLIST", "25544")
    monkeypatch.setenv("MIN_PEAK_ELEVATION_DEG", "30")
    monkeypatch.setenv("LEAD_MINUTES", "20")
    monkeypatch.setenv("DIGEST_LOOKAHEAD_HOURS", "72")
    monkeypatch.setattr(alerts_handler, "_ts", None)
    monkeypatch.setattr(alerts_handler, "_table", None)
    monkeypatch.setattr(alerts_handler, "_sns", None)
    monkeypatch.setattr(alerts_handler, "_observer", None)
    # Sentinel: compute_passes is stubbed in these tests, so the ephemeris
    # is passed around but never used — keeps tests free of the 17 MB file.
    monkeypatch.setattr(alerts_handler, "_eph", object())


def setup_aws(monkeypatch):
    """Create the moto table/parameter/topic and wire an SQS queue to the
    topic so tests can read exactly what SNS delivered."""
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
    table.put_item(Item=ISS_ITEM)

    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name="/sattrack/observer", Value="26.13,-80.23", Type="SecureString"
    )

    sns = boto3.client("sns", region_name="us-east-1")
    topic_arn = sns.create_topic(Name="test-alerts")["TopicArn"]
    monkeypatch.setenv("TOPIC_ARN", topic_arn)

    sqs = boto3.client("sqs", region_name="us-east-1")
    queue_url = sqs.create_queue(QueueName="test-alert-sink")["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]
    sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)
    return table, queue_url


def delivered_messages(queue_url) -> list[dict]:
    sqs = boto3.client("sqs", region_name="us-east-1")
    received = sqs.receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0
    ).get("Messages", [])
    return [json.loads(m["Body"]) for m in received]


def make_pass(rise: datetime, peak_deg=65.0, visible=True, duration_min=6):
    def iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "rise": iso(rise),
        "rise_direction": "NW",
        "culminate": iso(rise + timedelta(minutes=duration_min / 2)),
        "set": iso(rise + timedelta(minutes=duration_min)),
        "max_elevation_deg": peak_deg,
        "direction": "SSE",
        "visible": visible,
    }


def stub_passes(monkeypatch, passes):
    def fake_compute_passes(satellite, observer_lat, observer_lon, ts, eph,
                            start=None, hours=24.0, min_elevation_deg=10.0):
        return passes

    monkeypatch.setattr(alerts_handler, "compute_passes", fake_compute_passes)


# --- imminent mode -----------------------------------------------------------


@mock_aws
def test_imminent_alerts_for_pass_within_lead(monkeypatch):
    table, queue_url = setup_aws(monkeypatch)
    rise = datetime.now(timezone.utc) + timedelta(minutes=12)
    stub_passes(monkeypatch, [make_pass(rise)])

    result = alerts_handler.handler({"mode": "imminent"}, None)

    assert result["messages_published"] == 1
    (message,) = delivered_messages(queue_url)
    assert "ISS (ZARYA)" in message["Subject"]
    assert "65°" in message["Subject"]
    assert "Rises" in message["Message"]
    # Dedupe flag landed with a TTL a week past the pass.
    flag = table.get_item(
        Key={"pk": "25544", "sk": f"ALERT#{make_pass(rise)['rise']}"}
    )["Item"]
    assert int(flag["expires_at"]) > rise.timestamp()


@mock_aws
def test_imminent_skips_pass_beyond_lead(monkeypatch):
    _, queue_url = setup_aws(monkeypatch)
    rise = datetime.now(timezone.utc) + timedelta(minutes=45)
    stub_passes(monkeypatch, [make_pass(rise)])

    result = alerts_handler.handler({"mode": "imminent"}, None)

    assert result["messages_published"] == 0
    assert delivered_messages(queue_url) == []


@mock_aws
def test_imminent_does_not_repeat_on_next_tick(monkeypatch):
    _, queue_url = setup_aws(monkeypatch)
    rise = datetime.now(timezone.utc) + timedelta(minutes=12)
    stub_passes(monkeypatch, [make_pass(rise)])

    first = alerts_handler.handler({"mode": "imminent"}, None)
    second = alerts_handler.handler({"mode": "imminent"}, None)

    assert first["messages_published"] == 1
    assert second["messages_published"] == 0
    assert len(delivered_messages(queue_url)) == 1


@mock_aws
def test_imminent_filters_low_and_daylight_passes(monkeypatch):
    _, queue_url = setup_aws(monkeypatch)
    rise = datetime.now(timezone.utc) + timedelta(minutes=12)
    stub_passes(
        monkeypatch,
        [
            make_pass(rise, peak_deg=22.0),                # below the 30° bar
            make_pass(rise + timedelta(minutes=2), visible=False),  # daylight
        ],
    )

    result = alerts_handler.handler({"mode": "imminent"}, None)

    assert result["messages_published"] == 0
    assert delivered_messages(queue_url) == []


# --- digest mode -------------------------------------------------------------


@mock_aws
def test_digest_stays_silent_with_no_passes(monkeypatch):
    _, queue_url = setup_aws(monkeypatch)
    stub_passes(monkeypatch, [])

    result = alerts_handler.handler({"mode": "digest"}, None)

    assert result["messages_published"] == 0
    assert delivered_messages(queue_url) == []


@mock_aws
def test_digest_sends_one_message_listing_all_passes(monkeypatch):
    _, queue_url = setup_aws(monkeypatch)
    base = datetime.now(timezone.utc) + timedelta(hours=20)
    stub_passes(
        monkeypatch,
        [make_pass(base), make_pass(base + timedelta(days=1))],
    )

    result = alerts_handler.handler({"mode": "digest"}, None)

    assert result["messages_published"] == 1
    assert result["passes"] == 2
    (message,) = delivered_messages(queue_url)
    assert "2 good satellite passes" in message["Subject"]
    assert message["Message"].count("ISS (ZARYA)") == 2


@mock_aws
def test_digest_does_not_reannounce_next_day(monkeypatch):
    _, queue_url = setup_aws(monkeypatch)
    base = datetime.now(timezone.utc) + timedelta(hours=20)
    stub_passes(monkeypatch, [make_pass(base)])

    first = alerts_handler.handler({"mode": "digest"}, None)
    second = alerts_handler.handler({"mode": "digest"}, None)

    assert first["messages_published"] == 1
    assert second["messages_published"] == 0
    assert len(delivered_messages(queue_url)) == 1


@mock_aws
def test_digest_claim_does_not_block_imminent_alert(monkeypatch):
    _, queue_url = setup_aws(monkeypatch)
    rise = datetime.now(timezone.utc) + timedelta(minutes=12)
    stub_passes(monkeypatch, [make_pass(rise)])

    alerts_handler.handler({"mode": "digest"}, None)
    result = alerts_handler.handler({"mode": "imminent"}, None)

    assert result["messages_published"] == 1
    assert len(delivered_messages(queue_url)) == 2  # digest + reminder


# --- plumbing ----------------------------------------------------------------


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        alerts_handler.handler({"mode": "hourly"}, None)


def test_clock_formats_without_platform_flags():
    assert alerts_handler._clock(datetime(2026, 7, 18, 21, 14)) == "9:14 PM"
    assert alerts_handler._clock(datetime(2026, 7, 18, 0, 5)) == "12:05 AM"
