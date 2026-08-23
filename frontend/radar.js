/* RainViewer weather radar overlay — replaces an earlier NASA GIBS
 * true-color imagery attempt (removed): that layer is a full opaque
 * photograph of the whole Earth, which buried the observer location and
 * satellite dots underneath it. Radar tiles are transparent everywhere
 * there's no precipitation, so only actual weather shows up on top of
 * the existing globe — free, no API key, global coverage (1200+ radars,
 * per RainViewer's own docs), CORS-open and standard {z}/{x}/{y} Web
 * Mercator tiles confirmed directly against the live API before writing
 * this, so no custom tiling scheme is needed the way GIBS required one.
 */

const FRAMES_URL = "https://api.rainviewer.com/public/weather-maps.json";

// Radar-specific color scheme (2 = the common "universal blue" scheme
// RainViewer's own examples use) and options ("1_1" = smoothing + snow
// coloring on) — cosmetic choices, not load-bearing like the tile
// coordinates are.
const TILE_SIZE = 256;
const COLOR_SCHEME = 2;
const TILE_OPTIONS = "1_1";

export async function createRadarLayer() {
  const response = await fetch(FRAMES_URL);
  if (!response.ok) throw new Error(`RainViewer ${response.status}`);
  const body = await response.json();
  const frames = body.radar.past;
  const latest = frames[frames.length - 1];

  return new Cesium.UrlTemplateImageryProvider({
    url: `${body.host}${latest.path}/${TILE_SIZE}/{z}/{x}/{y}/${COLOR_SCHEME}/${TILE_OPTIONS}.png`,
    credit: new Cesium.Credit(
      'Radar: <a href="https://www.rainviewer.com" target="_blank" rel="noopener">RainViewer</a>'
    ),
  });
}
