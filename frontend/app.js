/* SatTrack globe — Phase 6: positions and passes are computed client-side
 * from raw TLEs (satellite.js/SGP4), not fetched pre-computed from the API.
 * Only the TLE catalog itself (GET /satellites) is polled, on a slow
 * cadence — everything else is local math tied to Cesium's own clock.
 *
 * Still no bundler: satellite.js loads as a native ES module import from a
 * CDN, which is why this file is loaded via <script type="module"> in
 * index.html (see the comment there for why that matters).
 */

"use strict";

import * as satellite from "https://cdn.jsdelivr.net/npm/satellite.js@7.1.0/+esm";

const config = window.SATTRACK_CONFIG || {};
const API = (config.apiBaseUrl || "").replace(/\/$/, "");

// TLEs only change on Phase 1's 2-hour fetch schedule and there's no
// CloudFront cache in front of this route yet (that's a later Phase 6
// step) — 30 min is a polite client cadence with headroom either way.
const CATALOG_REFRESH_MS = 30 * 60_000;
const PANEL_THROTTLE_MS = 1_000;

const statusEl = document.getElementById("status");

if (config.cesiumIonToken) {
  Cesium.Ion.defaultAccessToken = config.cesiumIonToken;
}

const viewer = new Cesium.Viewer("globe", {
  baseLayer: config.cesiumIonToken
    ? undefined
    : new Cesium.ImageryLayer(
        new Cesium.OpenStreetMapImageryProvider({
          url: "https://tile.openstreetmap.org/",
        })
      ),
  baseLayerPicker: false,
  geocoder: false,
  homeButton: false,
  sceneModePicker: false,
  navigationHelpButton: false,
  animation: false,
  timeline: false,
  infoBox: false,
  selectionIndicator: true,
});
viewer.scene.globe.enableLighting = true;

// The animation/timeline widgets are disabled above, but the clock still
// needs shouldAnimate=true to actually advance currentTime — without this,
// onTick still fires but currentTime never moves, so every satellite
// renders once and then silently never updates. This bit it Derek's app
// during development; leave it set.
viewer.clock.shouldAnimate = true;

function entityFor(sat) {
  const existing = viewer.entities.getById(sat.id);
  if (existing) return existing;
  return viewer.entities.add({
    id: sat.id,
    name: sat.name,
    point: {
      pixelSize: sat.id === "25544" ? 10 : 7,
      color:
        sat.id === "25544" ? Cesium.Color.GOLD : Cesium.Color.CYAN,
      outlineColor: Cesium.Color.BLACK,
      outlineWidth: 1,
    },
    label: {
      text: sat.name,
      font: "12px system-ui",
      fillColor: Cesium.Color.WHITE,
      outlineColor: Cesium.Color.BLACK,
      outlineWidth: 2,
      style: Cesium.LabelStyle.FILL_AND_OUTLINE,
      pixelOffset: new Cesium.Cartesian2(0, -14),
      verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
    },
  });
}

/* ---------- TLE catalog (slow refresh) ---------- */

const catalog = new Map(); // id -> { id, name, satrec } — named, interactive
let finderPopulated = false;

// Starlink-scale bulk group — visual only (see bulk swarm section below).
// A plain array + parallel PointPrimitive map, not the Entity API: no
// name lookup is needed per-point, so no reason to pay Entity overhead
// for ~10,000+ objects.
const bulkCatalog = []; // [{ id, satrec }]
const bulkPoints = new Map(); // id -> PointPrimitive
const bulkPointCollection = viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection());
let bulkCursor = 0; // rolling index into bulkCatalog, advanced in onTick below

async function refreshCatalog() {
  try {
    const response = await fetch(`${API}/satellites`);
    if (!response.ok) throw new Error(`API ${response.status}`);
    const body = await response.json();

    catalog.clear();
    bulkCatalog.length = 0;
    bulkPointCollection.removeAll();
    bulkPoints.clear();
    bulkCursor = 0;

    for (const sat of body.satellites) {
      try {
        const satrec = satellite.twoline2satrec(sat.line1, sat.line2);
        if (sat.group === "starlink") {
          bulkCatalog.push({ id: sat.id, satrec });
        } else {
          catalog.set(sat.id, { id: sat.id, name: sat.name, satrec });
        }
      } catch (err) {
        // One bad TLE shouldn't blank the whole globe. Starlink's
        // constant launch/deorbit churn exercises this far more than
        // the small, stable stations set ever did.
        console.warn(`skipping ${sat.name}: TLE parse failed`, err);
      }
    }

    // Real positions land over the next ~1s as the amortized onTick
    // loop below cycles through the full bulk list — a brief
    // "materializing" effect after each refresh, not worth avoiding.
    for (const sat of bulkCatalog) {
      bulkPoints.set(
        sat.id,
        bulkPointCollection.add({
          position: Cesium.Cartesian3.ZERO,
          color: Cesium.Color.LIGHTSKYBLUE.withAlpha(0.6),
          pixelSize: 2,
          id: sat.id,
        })
      );
    }

    statusEl.textContent = `${catalog.size} satellites tracked, ${bulkCatalog.length} in swarm · TLEs updated ${new Date().toLocaleTimeString()}`;
    populateFinder();
  } catch (err) {
    // Keep whatever catalog we already have — stale-but-working beats
    // blanking the globe over a transient network blip.
    statusEl.textContent = `TLE refresh failed (${err.message}) — showing last known data`;
  }
}

/* ---------- render loop (per-frame, driven by Cesium's own clock) ---------- */

const currentPositions = new Map(); // id -> { lat, lon, alt_km }
let lastPanelUpdateMs = 0;

// Naive per-frame propagation of ~10,769 bulk satellites would be
// roughly 648,000 SGP4 calls/second — well past a 60fps frame budget,
// before Cesium does any rendering work. Amortize instead: each tick
// advances a rolling cursor through a fixed-size slice of bulkCatalog,
// so the full set cycles through roughly once a second (self-adjusting
// to actual frame rate on slower hardware) with no single-frame stutter
// — a once-a-second full-batch update was tried first and rejected for
// exactly that stutter.
const BULK_SLICE_SIZE = 180; // ~10,769 / 60fps ≈ one full cycle per ~1s

viewer.clock.onTick.addEventListener((clock) => {
  const now = Cesium.JulianDate.toDate(clock.currentTime);
  const gmst = satellite.gstime(now);

  for (const sat of catalog.values()) {
    const posVel = satellite.propagate(sat.satrec, now);
    if (!posVel.position) continue; // decayed orbit / propagation error

    const geo = satellite.eciToGeodetic(posVel.position, gmst);
    const lat = satellite.degreesLat(geo.latitude);
    const lon = satellite.degreesLong(geo.longitude);
    const altKm = geo.height;

    currentPositions.set(sat.id, { lat, lon, alt_km: altKm });
    entityFor(sat).position = Cesium.Cartesian3.fromDegrees(lon, lat, altKm * 1000);
  }

  if (bulkCatalog.length > 0) {
    const sliceEnd = Math.min(bulkCursor + BULK_SLICE_SIZE, bulkCatalog.length);
    for (let i = bulkCursor; i < sliceEnd; i++) {
      const sat = bulkCatalog[i];
      const posVel = satellite.propagate(sat.satrec, now);
      if (!posVel.position) continue;

      const geo = satellite.eciToGeodetic(posVel.position, gmst);
      const point = bulkPoints.get(sat.id);
      if (point) {
        point.position = Cesium.Cartesian3.fromDegrees(
          satellite.degreesLong(geo.longitude),
          satellite.degreesLat(geo.latitude),
          geo.height * 1000
        );
      }
    }
    bulkCursor = sliceEnd >= bulkCatalog.length ? 0 : sliceEnd;
  }

  // Entities/points update every frame (or every amortized slice) for
  // smooth motion; the text panel doesn't need to redraw 60x/sec for
  // numbers that only usefully change ~1x/sec.
  const nowMs = Date.now();
  if (nowMs - lastPanelUpdateMs >= PANEL_THROTTLE_MS) {
    refreshPanel();
    lastPanelUpdateMs = nowMs;
  }
});

/* ---------- satellite finder ---------- */

const searchInput = document.getElementById("sat-search");
const searchOptions = document.getElementById("sat-options");

// Populated once per catalog refresh, not per tick — the list of tracked
// satellites doesn't change frame to frame.
function populateFinder() {
  if (finderPopulated || catalog.size === 0) return;
  const names = [...catalog.values()]
    .map((sat) => sat.name)
    .sort((a, b) => a.localeCompare(b));
  searchOptions.replaceChildren(
    ...names.map((name) => {
      const option = document.createElement("option");
      option.value = name;
      return option;
    })
  );
  finderPopulated = true;
}

searchInput.addEventListener("change", () => {
  const query = searchInput.value.trim().toLowerCase();
  if (!query) return;
  const sats = [...catalog.values()];
  const sat =
    sats.find((s) => s.name.toLowerCase() === query) ||
    sats.find((s) => s.name.toLowerCase().includes(query));
  if (!sat) return;

  const entity = viewer.entities.getById(sat.id);
  viewer.selectedEntity = entity;
  viewer.flyTo(entity, {
    offset: new Cesium.HeadingPitchRange(0, -Math.PI / 2, 2_500_000),
  });
  searchInput.blur();
});

/* ---------- selection panel ---------- */

const panel = document.getElementById("panel");
const passesList = document.getElementById("passes-list");
const passesButton = document.getElementById("passes-load");

function selectedSat() {
  const entity = viewer.selectedEntity;
  return entity ? catalog.get(entity.id) : undefined;
}

function refreshPanel() {
  const sat = selectedSat();
  if (!sat) {
    panel.hidden = true;
    return;
  }
  const pos = currentPositions.get(sat.id);
  if (!pos) return; // first tick hasn't run yet

  panel.hidden = false;
  document.getElementById("panel-name").textContent = sat.name;
  document.getElementById("panel-lat").textContent = `${pos.lat.toFixed(2)}°`;
  document.getElementById("panel-lon").textContent = `${pos.lon.toFixed(2)}°`;
  document.getElementById("panel-alt").textContent = `${pos.alt_km.toFixed(0)} km`;
}

viewer.selectedEntityChanged.addEventListener(() => {
  passesList.replaceChildren();
  refreshPanel(); // immediate on selection change, not throttle-delayed
});

/* ---------- observer location (Geolocation + manual entry) ---------- */

const OBSERVER_STORAGE_KEY = "sattrack-observer";
const obsActiveEl = document.getElementById("obs-active");
const obsManualForm = document.getElementById("obs-manual");
const obsLatInput = document.getElementById("obs-lat");
const obsLonInput = document.getElementById("obs-lon");

let activeObserver = null; // { lat, lon, source }

function setActiveObserver(lat, lon, source) {
  activeObserver = { lat, lon, source };
  localStorage.setItem(OBSERVER_STORAGE_KEY, JSON.stringify(activeObserver));
  obsActiveEl.textContent = `Observer: ${lat.toFixed(3)}°, ${lon.toFixed(3)}° (${source})`;
}

function loadPersistedObserver() {
  const raw = localStorage.getItem(OBSERVER_STORAGE_KEY);
  if (!raw) return;
  try {
    const { lat, lon, source } = JSON.parse(raw);
    setActiveObserver(lat, lon, source);
    obsLatInput.value = lat;
    obsLonInput.value = lon;
  } catch {
    // Corrupt/old localStorage value — ignore, user can re-enter.
  }
}

obsManualForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const lat = parseFloat(obsLatInput.value);
  const lon = parseFloat(obsLonInput.value);
  if (Number.isNaN(lat) || Number.isNaN(lon)) return;
  setActiveObserver(lat, lon, "manual");
});

/* ---------- local pass prediction ---------- */
/* Geometry only (rise/culminate/set, azimuth, elevation) — deliberately no
 * sunlit/twilight visibility classification yet, that's a later increment.
 * `visible: null` mirrors compute_passes(eph=None) in shared/passes.py,
 * which uses the same sentinel for "not classified yet". satellite.js has
 * no find_events() equivalent, so this is a simple time-stepped scan. */

const _COMPASS_POINTS = [
  "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
];

function azimuthToCompass(azimuthDeg) {
  return _COMPASS_POINTS[Math.round(azimuthDeg / 22.5) % 16];
}

function lookAngles(satrec, date, observerGd) {
  const posVel = satellite.propagate(satrec, date);
  if (!posVel.position) return null;
  const gmst = satellite.gstime(date);
  const positionEcf = satellite.eciToEcf(posVel.position, gmst);
  const look = satellite.ecfToLookAngles(observerGd, positionEcf);
  return {
    elevationDeg: satellite.radiansToDegrees(look.elevation),
    azimuthDeg: satellite.radiansToDegrees(look.azimuth),
  };
}

// Linear interpolation between the last below-threshold sample and the
// first above-threshold one (or vice versa) — good enough for "when to
// look up," not a scientific instrument.
function interpolateCrossing(a, b, thresholdDeg) {
  const span = b.elevationDeg - a.elevationDeg;
  if (span === 0) return a.time;
  const frac = (thresholdDeg - a.elevationDeg) / span;
  return new Date(a.time.getTime() + frac * (b.time.getTime() - a.time.getTime()));
}

function findPassesLocal(
  satrec,
  observerLatDeg,
  observerLonDeg,
  { hours = 48, minElevationDeg = 10, stepSeconds = 30 } = {}
) {
  const observerGd = {
    latitude: satellite.degreesToRadians(observerLatDeg),
    longitude: satellite.degreesToRadians(observerLonDeg),
    height: 0,
  };

  const start = new Date();
  const end = new Date(start.getTime() + hours * 3600_000);
  const passes = [];

  let prev = null; // { time, elevationDeg, azimuthDeg }
  let current = null; // in-progress pass

  for (let t = start; t <= end; t = new Date(t.getTime() + stepSeconds * 1000)) {
    const look = lookAngles(satrec, t, observerGd);
    if (!look) continue;
    const sample = { time: t, elevationDeg: look.elevationDeg, azimuthDeg: look.azimuthDeg };
    const above = look.elevationDeg >= minElevationDeg;

    if (above && !current) {
      if (prev === null) {
        // Pass already in progress at the very start of the window —
        // partial, dropped on set below (matches compute_passes()'s
        // "drop passes in progress at window edges").
        current = { rise: null, samples: [sample], partial: true };
      } else {
        const riseTime = interpolateCrossing(prev, sample, minElevationDeg);
        current = { rise: { time: riseTime, azimuthDeg: sample.azimuthDeg }, samples: [sample], partial: false };
      }
    } else if (above && current) {
      current.samples.push(sample);
    } else if (!above && current) {
      if (!current.partial) {
        const setTime = interpolateCrossing(prev, sample, minElevationDeg);
        const peak = current.samples.reduce((a, b) => (b.elevationDeg > a.elevationDeg ? b : a));
        passes.push({
          rise: current.rise.time.toISOString(),
          rise_direction: azimuthToCompass(current.rise.azimuthDeg),
          culminate: peak.time.toISOString(),
          set: setTime.toISOString(),
          max_elevation_deg: Math.round(peak.elevationDeg * 10) / 10,
          direction: azimuthToCompass(peak.azimuthDeg),
          visible: null,
        });
      }
      current = null;
    }

    prev = sample;
  }
  // A pass still above threshold when the window ends is partial too —
  // dropped by simply never pushing it (current just goes out of scope).

  return passes;
}

/* ---------- pass prediction UI ---------- */

function li(text, className) {
  const item = document.createElement("li");
  item.textContent = text;
  if (className) item.className = className;
  return item;
}

function renderPasses(passes) {
  if (!passes.length) {
    passesList.replaceChildren(li("No passes above 10° in the next 48 h."));
    return;
  }
  passesList.replaceChildren(
    ...passes.map((p) => {
      const peakTime = new Date(p.culminate).toLocaleString([], {
        weekday: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
      const setTime = new Date(p.set).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
      return li(
        `${peakTime} — peaks ${p.max_elevation_deg}° ${p.direction} ` +
          `(rises ${p.rise_direction}, sets ${setTime})`
      );
    })
  );
}

function computeAndRenderPasses(sat, lat, lon) {
  renderPasses(findPassesLocal(sat.satrec, lat, lon));
}

passesButton.addEventListener("click", () => {
  const sat = selectedSat();
  if (!sat) return;

  // No network round-trip anymore — if we already know where the viewer
  // is, skip the permission prompt entirely.
  if (activeObserver) {
    computeAndRenderPasses(sat, activeObserver.lat, activeObserver.lon);
    return;
  }

  passesButton.disabled = true;
  passesButton.textContent = "Locating…";

  navigator.geolocation.getCurrentPosition(
    (pos) => {
      const { latitude, longitude } = pos.coords;
      setActiveObserver(latitude, longitude, "browser");
      computeAndRenderPasses(sat, latitude, longitude);
      passesButton.disabled = false;
      passesButton.textContent = "Predict passes here (use my location)";
    },
    () => {
      passesList.replaceChildren(
        li("Location permission needed — or enter coordinates above.")
      );
      passesButton.disabled = false;
      passesButton.textContent = "Predict passes here (use my location)";
    },
    { timeout: 10_000 }
  );
});

/* ---------- go ---------- */

if (!API) {
  statusEl.textContent = "config.js missing apiBaseUrl";
} else {
  loadPersistedObserver();
  refreshCatalog();
  setInterval(refreshCatalog, CATALOG_REFRESH_MS);
}
