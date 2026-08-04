import json
from datetime import datetime, timezone

import pytest
from skyfield.api import EarthSatellite, load

from shared.passes import azimuth_to_compass, compute_passes, subpoint_of

ISS_TLE = (
    "ISS (ZARYA)",
    "1 25544U 98067A   26191.50000000  .00016717  00000-0  10270-3 0  9008",
    "2 25544  51.6423 339.8700 0007417  17.6667  85.6479 15.50423408123456",
)

# Fixed evaluation time near the TLE epoch keeps results deterministic.
FIXED_TIME = datetime(2026, 7, 16, 0, 0, 0, tzinfo=timezone.utc)

ts = load.timescale()


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


def test_visible_passes_are_json_serializable(tmp_path):
    """Run the real visibility branch (sunlit + darkness) against the same
    ephemeris the alerts Lambda uses, and require the result to survive
    json.dumps — numpy types leaking into the response is exactly the bug
    this caught."""
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
