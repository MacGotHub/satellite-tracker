/* SatTrack globe — polls the Phase 2 API and animates satellites on a
 * CesiumJS globe. Plain browser JS on purpose: no bundler means the whole
 * frontend is three static files behind CloudFront.
 */

"use strict";

const config = window.SATTRACK_CONFIG || {};
const API = (config.apiBaseUrl || "").replace(/\/$/, "");
const POLL_MS = 10_000;

const statusEl = document.getElementById("status");

// With an ion token Cesium serves its world satellite imagery; without one
// we fall back to OpenStreetMap tiles so the globe works before the token
// exists (it's a deploy-time config value, not a build requirement).
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

const latest = new Map(); // id -> last received position

async function poll() {
  try {
    const response = await fetch(`${API}/positions`);
    if (!response.ok) throw new Error(`API ${response.status}`);
    const body = await response.json();

    for (const sat of body.positions) {
      latest.set(sat.id, sat);
      entityFor(sat).position = Cesium.Cartesian3.fromDegrees(
        sat.lon,
        sat.lat,
        sat.alt_km * 1000
      );
    }
    statusEl.textContent = `${body.positions.length} satellites · updated ${new Date(
      body.time
    ).toLocaleTimeString()}`;
    refreshPanel();
  } catch (err) {
    statusEl.textContent = `API unreachable (${err.message}) — retrying`;
  }
}

/* ---------- selection panel + pass prediction ---------- */

const panel = document.getElementById("panel");
const passesList = document.getElementById("passes-list");
const passesButton = document.getElementById("passes-load");

function selectedSat() {
  const entity = viewer.selectedEntity;
  return entity ? latest.get(entity.id) : undefined;
}

function refreshPanel() {
  const sat = selectedSat();
  if (!sat) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  document.getElementById("panel-name").textContent = sat.name;
  document.getElementById("panel-lat").textContent = `${sat.lat.toFixed(2)}°`;
  document.getElementById("panel-lon").textContent = `${sat.lon.toFixed(2)}°`;
  document.getElementById("panel-alt").textContent = `${sat.alt_km.toFixed(0)} km`;
}

viewer.selectedEntityChanged.addEventListener(() => {
  passesList.replaceChildren();
  refreshPanel();
});

passesButton.addEventListener("click", () => {
  const sat = selectedSat();
  if (!sat) return;
  passesButton.disabled = true;
  passesButton.textContent = "Locating…";

  navigator.geolocation.getCurrentPosition(
    async (pos) => {
      // Coordinates go straight to the API as query params and nowhere else.
      const { latitude, longitude } = pos.coords;
      passesButton.textContent = "Predicting…";
      try {
        const response = await fetch(
          `${API}/satellites/${sat.id}/passes?lat=${latitude.toFixed(
            3
          )}&lon=${longitude.toFixed(3)}&hours=48`
        );
        const body = await response.json();
        renderPasses(body.passes || []);
      } catch {
        passesList.replaceChildren(li("Pass prediction failed — try again."));
      }
      passesButton.disabled = false;
      passesButton.textContent = "Predict passes here";
    },
    () => {
      passesList.replaceChildren(li("Location permission needed."));
      passesButton.disabled = false;
      passesButton.textContent = "Predict passes here";
    },
    { timeout: 10_000 }
  );
});

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
      const when = new Date(p.culminate).toLocaleString([], {
        weekday: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
      const item = li(
        `${when} — peaks ${p.max_elevation_deg}° ${p.direction}` +
          (p.visible ? " · VISIBLE" : " · not visible"),
        p.visible ? "visible-yes" : "visible-no"
      );
      return item;
    })
  );
}

/* ---------- go ---------- */

if (!API) {
  statusEl.textContent = "config.js missing apiBaseUrl";
} else {
  poll();
  setInterval(poll, POLL_MS);
}
