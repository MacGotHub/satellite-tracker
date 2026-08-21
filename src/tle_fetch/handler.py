import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

import boto3

from tle_fetch.celestrak_codes import OBJECT_TYPE_LABELS, OWNER_LABELS

CELESTRAK_URL_TEMPLATE = (
    "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
)

# Backlog: enrich the bare TLE catalog with real CelesTrak SATCAT facts
# (object type, owner, launch date) instead of hand-curated guesses — see
# DESIGN.md. Same GROUP query CelesTrak already buckets GP data by, so this
# rides the existing per-group loop instead of ~11k individual CATNR
# lookups, and the existing 2h schedule already matches CelesTrak's "checks
# for new data every 2 hours" policy for this endpoint too.
SATCAT_URL_TEMPLATE = "https://celestrak.org/satcat/records.php?GROUP={group}&FORMAT=json"

# DESIGN.md backlog item 1: reuse the table's existing TTL attribute (Phase
# 4's dedupe flags already rely on it) for TLE items too. Every successful
# fetch rewrites the full item, so a satellite CelesTrak keeps listing gets
# its expires_at pushed 7 days out on each ~2h cycle; one CelesTrak drops
# from a group simply stops being rewritten, and its last-set expires_at
# lets DynamoDB clear it within a week instead of it lingering forever with
# a stale orbit.
TLE_TTL_SECONDS = 7 * 24 * 60 * 60


def fetch_tle_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8")


def fetch_satcat_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8")


def parse_satcat(raw_json: str) -> dict[str, dict]:
    """Keyed by the same zero-padded 5-digit NORAD id parse_tle() derives
    from each TLE's line1[2:7] — SATCAT's NORAD_CAT_ID comes back as a bare
    JSON integer (e.g. 694, not "00694"), so this is the one place that
    padding has to be applied or every catalog number under 10000 would
    silently fail to match its TLE item."""
    records = json.loads(raw_json)
    satcat_by_id = {}
    for record in records:
        norad_id = str(record["NORAD_CAT_ID"]).zfill(5)
        object_type = record.get("OBJECT_TYPE")
        owner = record.get("OWNER")
        satcat_by_id[norad_id] = {
            "object_type": OBJECT_TYPE_LABELS.get(object_type, object_type),
            "owner": OWNER_LABELS.get(owner, owner),
            "launch_date": record.get("LAUNCH_DATE") or None,
            "decay_date": record.get("DECAY_DATE") or None,
            "rcs": record.get("RCS"),
        }
    return satcat_by_id


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


def write_satellites(table, satellites, fetched_at, group, satcat_by_id=None):
    satcat_by_id = satcat_by_id or {}
    expires_at = int(datetime.now(timezone.utc).timestamp()) + TLE_TTL_SECONDS
    with table.batch_writer() as batch:
        for sat in satellites:
            item = {
                "pk": sat["norad_id"],
                "sk": "TLE",
                "name": sat["name"],
                "line1": sat["line1"],
                "line2": sat["line2"],
                "fetched_at": fetched_at,
                "group": group,
                "expires_at": expires_at,
            }

            # Merged onto the same item, not a separate sk — SATCAT facts
            # are 1:1 with a satellite and change rarely, so there's no
            # reason to pay for a second item/write or a join on read.
            # Best-effort: a satellite CelesTrak's TLE feed still lists but
            # SATCAT dropped (or that group's SATCAT fetch failed this
            # cycle) simply keeps whatever it last had, rather than losing
            # its TLE over missing enrichment.
            satcat = satcat_by_id.get(sat["norad_id"])
            if satcat:
                if satcat["object_type"] is not None:
                    item["object_type"] = satcat["object_type"]
                if satcat["owner"] is not None:
                    item["owner"] = satcat["owner"]
                if satcat["launch_date"] is not None:
                    item["launch_date"] = satcat["launch_date"]
                if satcat["decay_date"] is not None:
                    item["decay_date"] = satcat["decay_date"]
                if satcat["rcs"] is not None:
                    # boto3's DynamoDB resource requires Decimal, not float,
                    # for Number attributes — str() first avoids binary
                    # float imprecision leaking into the stored value.
                    item["rcs"] = Decimal(str(satcat["rcs"]))

            batch.put_item(Item=item)


def handler(event, context):
    # Comma-separated, matching alerts.tf's WATCHLIST convention for
    # multi-value env config elsewhere in this project.
    groups = [g.strip() for g in os.environ.get("CELESTRAK_GROUP", "stations").split(",") if g.strip()]
    table_name = os.environ["TABLE_NAME"]
    bucket_name = os.environ["BUCKET_NAME"]

    dynamodb = boto3.resource("dynamodb")
    s3_client = boto3.client("s3")
    table = dynamodb.Table(table_name)

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    per_group = {}
    satcat_matched = {}
    archive_keys = {}
    for group in groups:
        url = CELESTRAK_URL_TEMPLATE.format(group=group)
        raw_text = fetch_tle_text(url)
        satellites = parse_tle(raw_text)

        if not satellites:
            raise ValueError(f"CelesTrak returned no TLEs for group '{group}'")

        # Best-effort and isolated from the TLE fetch above: SATCAT is a
        # separate CelesTrak endpoint with its own uptime, and losing
        # enrichment for one group on one cycle is far cheaper than losing
        # that group's actual position data over it.
        satcat_by_id = {}
        try:
            satcat_url = SATCAT_URL_TEMPLATE.format(group=group)
            satcat_by_id = parse_satcat(fetch_satcat_text(satcat_url))
        except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError) as exc:
            print(f"SATCAT fetch failed for group '{group}', skipping enrichment: {exc}")
        satcat_matched[group] = len(satcat_by_id)

        # Archive and write per group, immediately — if a later group's
        # fetch fails, earlier groups' data has already landed rather
        # than being discarded by a single merged write at the end.
        archive_keys[group] = archive_raw_tle(s3_client, bucket_name, group, raw_text, fetched_at)
        write_satellites(table, satellites, fetched_at, group, satcat_by_id)
        per_group[group] = len(satellites)

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "groups": groups,
                "satellite_count": sum(per_group.values()),
                "per_group": per_group,
                "satcat_matched": satcat_matched,
                "archive_keys": archive_keys,
                "fetched_at": fetched_at,
            }
        ),
    }
