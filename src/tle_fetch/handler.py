import json
import os
import urllib.request
from datetime import datetime, timezone

import boto3

CELESTRAK_URL_TEMPLATE = (
    "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
)


def fetch_tle_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8")


def parse_tle(raw_text: str) -> list[dict]:
    lines = [line.rstrip() for line in raw_text.strip().splitlines()]
    satellites = []
    for i in range(0, len(lines) - 2, 3):
        name, line1, line2 = lines[i], lines[i + 1], lines[i + 2]
        satellites.append(
            {
                "norad_id": line1[2:7].strip(),
                "name": name.strip(),
                "line1": line1,
                "line2": line2,
            }
        )
    return satellites


def archive_raw_tle(s3_client, bucket_name, group, raw_text, fetched_at):
    key = f"raw/{group}/{fetched_at}.tle"
    s3_client.put_object(Bucket=bucket_name, Key=key, Body=raw_text.encode("utf-8"))
    return key


def write_satellites(table, satellites, fetched_at):
    with table.batch_writer() as batch:
        for sat in satellites:
            batch.put_item(
                Item={
                    "pk": sat["norad_id"],
                    "sk": "TLE",
                    "name": sat["name"],
                    "line1": sat["line1"],
                    "line2": sat["line2"],
                    "fetched_at": fetched_at,
                }
            )


def handler(event, context):
    group = os.environ.get("CELESTRAK_GROUP", "stations")
    table_name = os.environ["TABLE_NAME"]
    bucket_name = os.environ["BUCKET_NAME"]

    dynamodb = boto3.resource("dynamodb")
    s3_client = boto3.client("s3")

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    url = CELESTRAK_URL_TEMPLATE.format(group=group)
    raw_text = fetch_tle_text(url)
    satellites = parse_tle(raw_text)

    if not satellites:
        raise ValueError(f"CelesTrak returned no TLEs for group '{group}'")

    archive_key = archive_raw_tle(s3_client, bucket_name, group, raw_text, fetched_at)
    write_satellites(dynamodb.Table(table_name), satellites, fetched_at)

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "group": group,
                "satellite_count": len(satellites),
                "archive_key": archive_key,
                "fetched_at": fetched_at,
            }
        ),
    }
