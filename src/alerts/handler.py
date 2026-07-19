"""Pass-alert Lambda — tells Derek when to walk outside.

Two modes, selected by the EventBridge Scheduler payload:

    {"mode": "imminent"}  every 10 minutes; publishes one message per visible
                          pass rising within the lead window (~15 min heads-up)
    {"mode": "digest"}    daily in the early evening; publishes a single
                          outlook message, and only when the lookahead window
                          actually contains alert-worthy passes

A pass is alert-worthy when shared.passes classifies it visible (satellite
sunlit + observer in darkness) AND its peak clears MIN_PEAK_ELEVATION_DEG.
Rise/set times are still computed against the 10-degree viewing horizon —
the peak bar decides IF we alert, not WHEN the pass starts.

Dedupe: one flag item per (satellite, mode, pass rise time) in the catalog
table, written with a conditional put BEFORE publishing. Flag-first makes
alerts at-most-once by design: a crash between flag and publish drops that
one message, which beats duplicate texts once SMS (billed per message) joins
the topic. TTL clears the flags a week after the pass.

Observer coordinates come from SSM at cold start (never code or env config —
this repo is portfolio-bound and home coordinates stay out of it).
"""

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
from boto3.dynamodb.conditions import Attr
from skyfield.api import EarthSatellite, load
from skyfield.iokit import load_file

from shared.passes import compute_passes

# Rise/set threshold for humans: below ~10 deg a pass hides behind trees and
# rooflines. The alert bar (peak elevation) is configured separately.
VIEWING_HORIZON_DEG = 10.0

DEDUPE_TTL_DAYS = 7

# Alert copy is written for reading on a phone in the backyard, so times are
# local to the observer. tzdata ships in the Lambda layer for zoneinfo.
LOCAL_TZ = ZoneInfo("America/New_York")
LOCAL_TZ_LABEL = "Eastern"

# Module-level caches, same pattern as the API Lambda: the execution
# environment survives between invocations, cold-start work shouldn't repeat.
_ts = None
_eph = None
_table = None
_sns = None
_observer = None


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


def _sns_client():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    return _sns


def _observer_coords() -> tuple[float, float]:
    global _observer
    if _observer is None:
        value = boto3.client("ssm").get_parameter(
            Name=os.environ["OBSERVER_PARAM"], WithDecryption=True
        )["Parameter"]["Value"]
        lat_text, lon_text = value.split(",")
        _observer = (float(lat_text), float(lon_text))
    return _observer


def _visible_passes(start: datetime, hours: float) -> list[dict]:
    """Alert-worthy passes for every watchlist satellite, soonest first."""
    lat, lon = _observer_coords()
    min_peak = float(os.environ["MIN_PEAK_ELEVATION_DEG"])

    found = []
    for norad_id in os.environ["WATCHLIST"].split(","):
        norad_id = norad_id.strip()
        item = _catalog_table().get_item(
            Key={"pk": norad_id, "sk": "TLE"}
        ).get("Item")
        if item is None:
            # A missing TLE is a pipeline problem, not an alerting one —
            # log and keep checking the rest of the watchlist.
            print(f"watchlist satellite {norad_id} not in catalog; skipping")
            continue

        satellite = EarthSatellite(
            item["line1"], item["line2"], item["name"], _timescale()
        )
        for p in compute_passes(
            satellite,
            observer_lat=lat,
            observer_lon=lon,
            ts=_timescale(),
            eph=_ephemeris(),
            start=start,
            hours=hours,
            min_elevation_deg=VIEWING_HORIZON_DEG,
        ):
            if p["visible"] and p["max_elevation_deg"] >= min_peak:
                found.append({"norad_id": item["pk"], "name": item["name"], **p})

    found.sort(key=lambda p: p["rise"])
    return found


def _claim(kind: str, norad_id: str, rise_iso: str) -> bool:
    """Atomically claim a pass for `kind` ("ALERT"/"DIGEST"); False if taken."""
    rise = _parse_iso(rise_iso)
    try:
        _catalog_table().put_item(
            Item={
                "pk": norad_id,
                "sk": f"{kind}#{rise_iso}",
                "expires_at": int(
                    (rise + timedelta(days=DEDUPE_TTL_DAYS)).timestamp()
                ),
            },
            ConditionExpression=Attr("pk").not_exists(),
        )
        return True
    except _catalog_table().meta.client.exceptions.ConditionalCheckFailedException:
        return False


def _publish(subject: str, message: str) -> None:
    _sns_client().publish(
        TopicArn=os.environ["TOPIC_ARN"],
        Subject=subject[:100],  # SNS hard limit
        Message=message,
    )


# --- message formatting ------------------------------------------------------


def _parse_iso(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _local(iso: str) -> datetime:
    return _parse_iso(iso).astimezone(LOCAL_TZ)


def _clock(dt: datetime) -> str:
    # strftime's no-leading-zero hour flag is platform-specific (%-I vs %#I),
    # so format by hand — tests run on Windows, Lambda is Linux.
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'PM' if dt.hour >= 12 else 'AM'}"


def _day(dt: datetime) -> str:
    return f"{dt.strftime('%a %b')} {dt.day}"


def _pass_subject(p: dict) -> str:
    rise = _local(p["rise"])
    return (
        f"{p['name']} pass at {_clock(rise)} — "
        f"up to {p['max_elevation_deg']:.0f}° in the {p['direction']}"
    )


def _pass_body(p: dict) -> str:
    rise, culminate, sets = _local(p["rise"]), _local(p["culminate"]), _local(p["set"])
    return (
        f"{p['name']} visible pass:\n"
        f"\n"
        f"  Rises {_clock(rise)} in the {p['rise_direction']}\n"
        f"  Peaks {p['max_elevation_deg']:.0f}° above the horizon "
        f"({p['direction']}) at {_clock(culminate)}\n"
        f"  Sets {_clock(sets)}\n"
        f"\n"
        f"Times are {LOCAL_TZ_LABEL}, {_day(rise)}. Head out a few minutes early."
    )


def _digest_subject(passes: list[dict], hours: float) -> str:
    count = len(passes)
    days = max(1, round(hours / 24))
    plural = "es" if count != 1 else ""
    return f"{count} good satellite pass{plural} in the next {days} days"


def _digest_body(passes: list[dict]) -> str:
    lines = ["Upcoming visible passes worth planning around:", ""]
    for p in passes:
        rise = _local(p["rise"])
        lines.append(
            f"  {_day(rise)} — {p['name']}: rises {_clock(rise)} "
            f"{p['rise_direction']}, peaks {p['max_elevation_deg']:.0f}° "
            f"{p['direction']} at {_clock(_local(p['culminate']))}"
        )
    lines += ["", f"Times are {LOCAL_TZ_LABEL}. Each pass gets its own reminder ~15 minutes out."]
    return "\n".join(lines)


# --- modes -------------------------------------------------------------------


def _imminent(now: datetime) -> dict:
    lead = timedelta(minutes=float(os.environ["LEAD_MINUTES"]))
    # Search past the lead window: compute_passes drops passes whose SET
    # event falls outside the search window, and a pass that rises at the
    # far edge of the lead still needs its whole arc (<~15 min) inside.
    search_hours = (lead + timedelta(minutes=30)) / timedelta(hours=1)

    sent = 0
    for p in _visible_passes(now, search_hours):
        if _parse_iso(p["rise"]) - now > lead:
            continue
        if not _claim("ALERT", p["norad_id"], p["rise"]):
            continue
        _publish(_pass_subject(p), _pass_body(p))
        sent += 1
    return {"mode": "imminent", "messages_published": sent}


def _digest(now: datetime) -> dict:
    hours = float(os.environ["DIGEST_LOOKAHEAD_HOURS"])
    # Claim per pass so tomorrow's digest only announces NEW passes — and
    # stays silent (no message at all) when there's nothing new to say.
    fresh = [
        p
        for p in _visible_passes(now, hours)
        if _claim("DIGEST", p["norad_id"], p["rise"])
    ]
    if not fresh:
        return {"mode": "digest", "messages_published": 0}

    _publish(_digest_subject(fresh, hours), _digest_body(fresh))
    return {"mode": "digest", "messages_published": 1, "passes": len(fresh)}


def handler(event, context):
    mode = event.get("mode", "imminent")
    now = datetime.now(timezone.utc)

    if mode == "imminent":
        result = _imminent(now)
    elif mode == "digest":
        result = _digest(now)
    else:
        raise ValueError(f"unknown mode '{mode}'")

    print(json.dumps(result))
    return result
