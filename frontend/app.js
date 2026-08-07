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
import { sunPosition, sunAltitudeDeg, isSunlit } from "./sun.js";

const config = window.SATTRACK_CONFIG || {};
const API = (config.apiBaseUrl || "").replace(/\/$/, "");

// TLEs only change on Phase 1's 2-hour fetch schedule, and Phase 6 Step 4
// put a 1-2h CloudFront cache in front of this route — 30 min is a polite
// client cadence with headroom either way.
const CATALOG_REFRESH_MS = 30 * 60_000;
const PANEL_THROTTLE_MS = 1_000;

// ISS-class visible passes arrive in clusters separated by weeks, so a
// short window legitimately returns "no passes" most of the time —
// propagating one satellite over two weeks is milliseconds, so there's no
// real cost to widening it. Keep index.html's passes-hint text in sync if
// this changes.
const PASS_SEARCH_DAYS = 14;

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

// A satellite that can't propagate at one instant usually can't propagate
// at any nearby instant either, so this would otherwise re-fire every tick
// (60x/sec) for as long as that condition holds. Warn once per satellite
// per catalog refresh instead of flooding the console forever.
const warnedCatalogSkips = new Set();

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
    warnedCatalogSkips.clear();
    bulkCatalog.length = 0;
    bulkPointCollection.removeAll();
    bulkPoints.clear();
    bulkCursor = 0;

    for (const sat of body.satellites) {
      try {
        const satrec = satellite.twoline2satrec(sat.line1, sat.line2);
        // twoline2satrec doesn't throw on a bad element set — it sets
        // satrec.error instead (out-of-range eccentricity, etc.). Catch
        // it here, at load time, so a dead-on-arrival satrec never enters
        // the render set and gets re-tried every propagation cycle.
        if (satrec.error) {
          throw new Error(`sgp4init error code ${satrec.error}`);
        }
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

// satellite.propagate() returns { position: false, velocity: false } on a
// hard SGP4 failure (decayed orbit, etc.) — but a small fraction of edge-case
// orbital elements make SGP4 "succeed" while returning NaN-filled ECI
// components instead of tripping that flag. A truthy-but-NaN position slips
// past a plain `!posVel.position` check, and a NaN Cartesian3 fed into
// Cesium's PointPrimitiveCollection crashes its internal bounding-volume
// math with an unrelated-looking "Cannot read properties of null" error —
// this is what actually broke the swarm render. Check finiteness, not just
// truthiness.
function hasValidPosition(posVel) {
  // propagate() itself can come back null (not just { position: false })
  // for some satrecs — confirmed live against the full Starlink swarm,
  // not just a theoretical case. Guard posVel before touching .position.
  const p = posVel && posVel.position;
  return (
    !!p &&
    Number.isFinite(p.x) &&
    Number.isFinite(p.y) &&
    Number.isFinite(p.z)
  );
}

let bulkSkippedThisCycle = 0;

viewer.clock.onTick.addEventListener((clock) => {
  const now = Cesium.JulianDate.toDate(clock.currentTime);
  const gmst = satellite.gstime(now);

  for (const sat of catalog.values()) {
    const posVel = satellite.propagate(sat.satrec, now);
    if (!hasValidPosition(posVel)) {
      if (!warnedCatalogSkips.has(sat.id)) {
        console.warn(`skipping ${sat.name}: no valid propagated position`);
        warnedCatalogSkips.add(sat.id);
      }
      continue;
    }

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
      if (!hasValidPosition(posVel)) {
        bulkSkippedThisCycle++;
        continue;
      }

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

    const cycleComplete = sliceEnd >= bulkCatalog.length;
    if (cycleComplete) {
      // Log once per ~1s full-swarm cycle, not per skip — at 10k+ objects
      // a handful of unpropagatable ones (decayed/malformed elements) is
      // normal SGP4 behavior. A large or growing count instead points at
      // bad TLE ingestion upstream, not routine propagation noise.
      if (bulkSkippedThisCycle > 0) {
        const pct = ((bulkSkippedThisCycle / bulkCatalog.length) * 100).toFixed(1);
        console.warn(
          `swarm propagation: skipped ${bulkSkippedThisCycle}/${bulkCatalog.length} objects this cycle (${pct}%)`
        );
      }
      bulkSkippedThisCycle = 0;
    }
    bulkCursor = cycleComplete ? 0 : sliceEnd;
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
const passesShowAllInput = document.getElementById("passes-show-all");
const passesHeroEl = document.getElementById("passes-hero");

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
  passesHeroEl.hidden = true; // stale for the newly-selected satellite
  lastComputedPasses = null; // stale for the newly-selected satellite
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
/* Rise/culminate/set, azimuth, elevation, and (DESIGN.md backlog item 8)
 * a visibility verdict — satellite.js has no find_events() equivalent, so
 * this is a simple time-stepped scan, not a root-finder. */

// Sun altitude below which the ground observer counts as "in darkness" —
// civil twilight. Must match OBSERVER_DARK_SUN_ALTITUDE_DEG in
// shared/passes.py exactly: same threshold, deliberately duplicated
// across the two runtimes (alerts Lambda vs. browser) rather than shared,
// per Phase 6's own "two engines by design" carve-out.
const OBSERVER_DARK_SUN_ALTITUDE_DEG = -6.0;

const _COMPASS_POINTS = [
  "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
];

function azimuthToCompass(azimuthDeg) {
  return _COMPASS_POINTS[Math.round(azimuthDeg / 22.5) % 16];
}

// Daylight is checked before eclipsed: a bright sky blocks every
// satellite equally, while eclipsed is specific to this one satellite at
// this one instant — daylight is the more fundamental, more informative
// reason when both happen to be true at once.
function classifyVisibility(satrec, peakTime, observerLatDeg, observerLonDeg) {
  const posVel = satellite.propagate(satrec, peakTime);
  if (!hasValidPosition(posVel)) return { visible: null, reason: null };

  const sun = sunPosition(peakTime);
  const gmst = satellite.gstime(peakTime);
  const observerSunAltDeg = sunAltitudeDeg(sun, observerLatDeg, observerLonDeg, gmst);

  if (observerSunAltDeg >= OBSERVER_DARK_SUN_ALTITUDE_DEG) {
    return { visible: false, reason: "daylight" };
  }
  if (!isSunlit(posVel.position, sun)) {
    return { visible: false, reason: "eclipsed" };
  }
  return { visible: true, reason: null };
}

function lookAngles(satrec, date, observerGd) {
  const posVel = satellite.propagate(satrec, date);
  if (!hasValidPosition(posVel)) return null;
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
  { hours = PASS_SEARCH_DAYS * 24, minElevationDeg = 10, stepSeconds = 30 } = {}
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
        const { visible, reason } = classifyVisibility(
          satrec,
          peak.time,
          observerLatDeg,
          observerLonDeg
        );
        passes.push({
          rise: current.rise.time.toISOString(),
          rise_direction: azimuthToCompass(current.rise.azimuthDeg),
          culminate: peak.time.toISOString(),
          set: setTime.toISOString(),
          max_elevation_deg: Math.round(peak.elevationDeg * 10) / 10,
          direction: azimuthToCompass(peak.azimuthDeg),
          visible,
          reason,
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

// null (not yet computed for the currently-selected satellite) vs. []
// (computed, genuinely zero passes) matter differently to the "show all"
// toggle handler below — keep them distinguishable, don't collapse to [].
let lastComputedPasses = null;

const VISIBILITY_REASON_LABELS = {
  daylight: "daylight",
  eclipsed: "satellite in shadow",
};

function passLabel(p) {
  // month/day, not just weekday: over a multi-week window "Mon" alone is
  // ambiguous — there can be two of them.
  const peakTime = new Date(p.culminate).toLocaleString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
  const setTime = new Date(p.set).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
  const base = `${peakTime} — peaks ${p.max_elevation_deg}° ${p.direction} ` +
    `(rises ${p.rise_direction}, sets ${setTime})`;
  return p.visible ? base : `${base} — ${VISIBILITY_REASON_LABELS[p.reason] || "not visible"}`;
}

// DESIGN.md backlog item 9: "next visible pass" is the question users are
// actually asking — surface it above the full list instead of making them
// scan for it. Independent of the "show all passes" toggle: the hero always
// answers the visible-pass question, even while the list below is showing
// everything.
function renderHero(passes) {
  const next = passes.find((p) => p.visible);
  if (!next) {
    // Honest empty case, not just silence — visible passes arrive in
    // clusters separated by weeks, so "none in the window" is routine,
    // not a sign anything is broken.
    passesHeroEl.textContent =
      `No visible passes in the next ${PASS_SEARCH_DAYS} days — visibility ` +
      `comes in clusters, so this is normal. Check back in a few days.`;
    passesHeroEl.className = "hero-empty";
  } else {
    passesHeroEl.textContent =
      `Next visible pass: ${passLabel(next)}`;
    passesHeroEl.className = "hero-active";
  }
  passesHeroEl.hidden = false;
}

function renderPasses(passes) {
  lastComputedPasses = passes;
  renderHero(passes);
  const showAll = passesShowAllInput.checked;
  const shown = showAll ? passes : passes.filter((p) => p.visible);

  if (!passes.length) {
    passesList.replaceChildren(
      li(`No passes above 10° in the next ${PASS_SEARCH_DAYS} days.`)
    );
    return;
  }
  if (!shown.length) {
    // Passes exist but none are visible — a materially different empty
    // case from "nothing overhead at all" above; DESIGN.md item 8 asks
    // for the verdict to read as *explaining the sky*, not just going
    // quiet the same way an empty catalog would.
    passesList.replaceChildren(
      li(
        `${passes.length} pass${passes.length === 1 ? "" : "es"} in the ` +
          `next ${PASS_SEARCH_DAYS} days, but none visible (daylight or ` +
          `Earth's shadow). Check "show all passes" to see them.`
      )
    );
    return;
  }
  passesList.replaceChildren(
    ...shown.map((p) => li(passLabel(p), p.visible ? undefined : "pass-dim"))
  );
}

// Re-filter the already-computed list instead of re-running the (now up
// to 14-day) propagation scan just to flip a display filter.
passesShowAllInput.addEventListener("change", () => {
  if (lastComputedPasses === null) return; // nothing computed yet
  renderPasses(lastComputedPasses);
});

const PASSES_BUTTON_DEFAULT_LABEL = "Predict passes here (use my location)";

function computeAndRenderPasses(sat, lat, lon) {
  passesButton.disabled = true;
  passesButton.textContent = "Computing…";
  // Defer to the next paint so "Computing…" actually becomes visible
  // before the synchronous propagation loop runs — at PASS_SEARCH_DAYS's
  // 14-day window (30s steps, ~40k satellite.propagate() calls) this is
  // long enough to freeze the tab for a moment without this, which read
  // as a hang rather than a computation in progress.
  requestAnimationFrame(() => {
    renderPasses(findPassesLocal(sat.satrec, lat, lon));
    passesButton.disabled = false;
    passesButton.textContent = PASSES_BUTTON_DEFAULT_LABEL;
  });
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
    },
    () => {
      passesList.replaceChildren(
        li("Location permission needed — or enter coordinates above.")
      );
      passesButton.disabled = false;
      passesButton.textContent = PASSES_BUTTON_DEFAULT_LABEL;
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
