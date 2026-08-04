/* Low-precision solar position and satellite-eclipse math for client-side
 * pass visibility classification (DESIGN.md backlog item 8). satellite.js
 * has no equivalent of Skyfield's is_sunlit()/observe(sun) — this ports
 * the same two checks src/shared/passes.py already does server-side (for
 * the alerts Lambda) to a runtime that doesn't carry a JPL ephemeris.
 *
 * Sun position: the standard "low accuracy" formula from the Astronomical
 * Almanac (good to ~0.01 deg, valid roughly 1950-2050) — plenty of
 * precision for whole-degree twilight thresholds and a shadow-cylinder
 * test against Earth's ~6371 km radius. No ephemeris download needed,
 * unlike the Skyfield/de421 path this mirrors.
 *
 * Frame note: this produces an equatorial-of-date direction, used
 * directly against satellite.js's TEME (True Equator Mean Equinox)
 * position vectors as if they were the same frame. The actual mismatch
 * between TEME and true-equator-of-date is arcseconds to arcminutes —
 * irrelevant next to whole-degree twilight thresholds and a shadow test
 * against a 6378 km radius.
 */

const DEG2RAD = Math.PI / 180;
const RAD2DEG = 180 / Math.PI;

// WGS84 equatorial radius — matches the constant satellite.js itself uses
// internally for its own Earth-radius-dependent math.
const EARTH_RADIUS_KM = 6378.137;

function normalizeDeg(deg) {
  return ((deg % 360) + 360) % 360;
}

// Geocentric equatorial-of-date direction to the Sun at `date`: right
// ascension and declination (radians), plus a unit vector in the same
// Earth-centered {x,y,z} km convention satellite.js ECI positions use —
// direction only, since the Sun's actual distance doesn't matter for
// either check below.
function sunPosition(date) {
  const jd = date.getTime() / 86_400_000 + 2440587.5; // Unix epoch -> Julian Date
  const n = jd - 2451545.0; // days since J2000.0

  const meanLongitudeDeg = normalizeDeg(280.46 + 0.9856474 * n);
  const meanAnomalyDeg = normalizeDeg(357.528 + 0.9856003 * n);
  const g = meanAnomalyDeg * DEG2RAD;

  const eclipticLongitudeDeg = normalizeDeg(
    meanLongitudeDeg + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)
  );
  const lambda = eclipticLongitudeDeg * DEG2RAD;
  const obliquityDeg = 23.439 - 0.0000004 * n;
  const epsilon = obliquityDeg * DEG2RAD;

  const rightAscension = Math.atan2(
    Math.cos(epsilon) * Math.sin(lambda),
    Math.cos(lambda)
  );
  const declination = Math.asin(Math.sin(epsilon) * Math.sin(lambda));

  return {
    rightAscension,
    declination,
    unitVector: {
      x: Math.cos(declination) * Math.cos(rightAscension),
      y: Math.cos(declination) * Math.sin(rightAscension),
      z: Math.sin(declination),
    },
  };
}

// Sun's altitude in degrees above an observer's horizon — the "is the sky
// dark enough to see a satellite" check. `gmstRad` is satellite.js's own
// satellite.gstime(date) output, reused rather than recomputed.
function sunAltitudeDeg(sun, observerLatDeg, observerLonDeg, gmstRad) {
  const lat = observerLatDeg * DEG2RAD;
  const localSiderealTime = gmstRad + observerLonDeg * DEG2RAD;
  // Normalize the hour angle to [-pi, pi] so it stays well-conditioned
  // near the 0/2*pi wraparound.
  const hourAngle = Math.atan2(
    Math.sin(localSiderealTime - sun.rightAscension),
    Math.cos(localSiderealTime - sun.rightAscension)
  );

  const sinAlt =
    Math.sin(lat) * Math.sin(sun.declination) +
    Math.cos(lat) * Math.cos(sun.declination) * Math.cos(hourAngle);
  return Math.asin(sinAlt) * RAD2DEG;
}

// Cylindrical Earth-shadow model: is `eciPositionKm` (a satellite.js ECI
// position, {x,y,z} in km) sunlit, or in Earth's shadow? Standard LEO
// simplification — treats the shadow as an infinite cylinder along the
// anti-sun direction rather than modeling the actual umbra/penumbra cone;
// the cone's taper is negligible at LEO ranges relative to Earth's radius.
function isSunlit(eciPositionKm, sun) {
  const s = sun.unitVector;
  const r = eciPositionKm;
  const sunwardDistance = r.x * s.x + r.y * s.y + r.z * s.z;
  if (sunwardDistance > 0) return true; // sunward side of Earth's center

  const perp = {
    x: r.x - sunwardDistance * s.x,
    y: r.y - sunwardDistance * s.y,
    z: r.z - sunwardDistance * s.z,
  };
  const perpDistanceKm = Math.sqrt(
    perp.x * perp.x + perp.y * perp.y + perp.z * perp.z
  );
  return perpDistanceKm >= EARTH_RADIUS_KM;
}

export { sunPosition, sunAltitudeDeg, isSunlit };
