"""Satellite pass prediction and visibility logic.

Shared between the Phase 2 API (GET /satellites/{id}/passes) and the Phase 4
alerts Lambda — both answer the same question ("when is this satellite worth
walking outside for?"), so the math lives here once instead of forking.

A pass is *visible* when all three hold at culmination:
  1. the satellite is sunlit (still catching sunlight at altitude),
  2. the observer is in darkness (sun below civil twilight, -6 deg), and
  3. the pass peaks above the caller's minimum elevation (find_events
     already guarantees this by construction).
"""

from datetime import datetime, timedelta, timezone

from skyfield.api import wgs84

# Sun altitude below which the ground observer counts as "in darkness".
# -6 deg is civil twilight — bright satellites become visible around then;
# waiting for full astronomical darkness (-18 deg) would drop real sightings.
OBSERVER_DARK_SUN_ALTITUDE_DEG = -6.0

_COMPASS_POINTS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def azimuth_to_compass(azimuth_deg: float) -> str:
    return _COMPASS_POINTS[round(azimuth_deg / 22.5) % 16]


def subpoint_of(satellite, t) -> dict:
    """Current geodetic position (lat/lon/alt) of a satellite at time t."""
    geocentric = satellite.at(t)
    position = wgs84.geographic_position_of(geocentric)
    return {
        "lat": round(position.latitude.degrees, 4),
        "lon": round(position.longitude.degrees, 4),
        "alt_km": round(position.elevation.km, 1),
    }


def compute_passes(
    satellite,
    observer_lat: float,
    observer_lon: float,
    ts,
    eph,
    start: datetime | None = None,
    hours: float = 24.0,
    min_elevation_deg: float = 10.0,
) -> list[dict]:
    """Predict passes of `satellite` over an observer location.

    Returns one dict per pass (rise/culmination/set times, peak elevation,
    compass directions, and a `visible` verdict). Passes already in progress
    at the window edges are dropped — a partial pass isn't alert-worthy.

    `eph` is a JPL ephemeris (de421) used for the sunlit/darkness checks;
    pass None to skip visibility classification (`visible` becomes None),
    which keeps unit tests free of the 17 MB ephemeris download.
    """
    observer = wgs84.latlon(observer_lat, observer_lon)
    start = start or datetime.now(timezone.utc)
    t0 = ts.from_datetime(start)
    t1 = ts.from_datetime(start + timedelta(hours=hours))

    times, events = satellite.find_events(
        observer, t0, t1, altitude_degrees=min_elevation_deg
    )

    passes = []
    current: dict | None = None
    for t, event in zip(times, events):
        if event == 0:  # rise above min elevation
            current = {"rise": t}
        elif event == 1 and current is not None:  # culmination
            current["culminate"] = t
        elif event == 2 and current is not None and "culminate" in current:
            current["set"] = t
            passes.append(_describe_pass(satellite, observer, current, eph))
            current = None

    return passes


def _describe_pass(satellite, observer, events: dict, eph) -> dict:
    topocentric = (satellite - observer).at(events["culminate"])
    alt, az, _ = topocentric.altaz()
    rise_az = (satellite - observer).at(events["rise"]).altaz()[1]

    visible = None
    if eph is not None:
        sunlit = satellite.at(events["culminate"]).is_sunlit(eph)
        observer_dark = _sun_altitude_deg(eph, observer, events["culminate"]) \
            < OBSERVER_DARK_SUN_ALTITUDE_DEG
        # bool() matters: both operands are numpy bools, and json.dumps
        # refuses numpy.bool_ (it is not a subclass of Python bool).
        visible = bool(sunlit and observer_dark)

    return {
        "rise": events["rise"].utc_iso(),
        "rise_direction": azimuth_to_compass(rise_az.degrees),
        "culminate": events["culminate"].utc_iso(),
        "set": events["set"].utc_iso(),
        "max_elevation_deg": round(alt.degrees, 1),
        "direction": azimuth_to_compass(az.degrees),
        "visible": visible,
    }


def _sun_altitude_deg(eph, observer, t) -> float:
    location = eph["earth"] + observer
    alt, _, _ = location.at(t).observe(eph["sun"]).apparent().altaz()
    return alt.degrees
