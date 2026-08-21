/* Static "what is this" blurbs — the detail panel previously showed only
 * the bare catalog name, leaving users to look everything up themselves.
 * Specific blurbs are hand-curated only for names we can describe
 * confidently (the well-known crewed-station spacecraft in CelesTrak's
 * "stations" group). Everything else — including CelesTrak's broader
 * "visual" group, which is mostly ordinary satellites, spent rocket
 * bodies, and small university cubesats we have no reliable facts about —
 * falls back to a name-pattern category or no blurb at all, rather than
 * guessing at a specific object's mission.
 */

const KNOWN_BLURBS = {
  "ISS (ZARYA)":
    "The International Space Station's first module, launched in 1998 — now a structural and propulsion element, not used for cargo.",
  "ISS (NAUKA)":
    "Roscosmos's Multipurpose Laboratory Module on the ISS, added in 2021.",
  "CSS (TIANHE)": "The core module of China's Tiangong space station.",
  "CSS (WENTIAN)":
    "A science laboratory module on China's Tiangong space station.",
  "CSS (MENGTIAN)":
    "A science laboratory module on China's Tiangong space station.",
  "CYGNUS NG-24":
    "A Northrop Grumman Cygnus cargo spacecraft on a resupply run to the ISS.",
  "CREW DRAGON 12":
    "A SpaceX Crew Dragon capsule carrying astronauts to and from the ISS.",
  "PROGRESS-MS 33":
    "A Russian Progress-MS uncrewed cargo spacecraft resupplying the ISS.",
  "PROGRESS-MS 34":
    "A Russian Progress-MS uncrewed cargo spacecraft resupplying the ISS.",
  "SOYUZ-MS 28":
    "A Russian Soyuz-MS spacecraft carrying crew to and from the ISS.",
  "SHENZHOU-23 (SZ-23)":
    "A Chinese Shenzhou crewed spacecraft carrying astronauts to Tiangong.",
  "TIANZHOU-10":
    "A Chinese Tianzhou cargo spacecraft resupplying Tiangong.",
};

// Name-pattern fallback for anything not individually curated above —
// these read off the name itself rather than asserting a specific
// object's identity, so they stay accurate even for names we don't
// otherwise recognize.
const PATTERN_BLURBS = [
  {
    pattern: /\bR\/B\b/i,
    blurb:
      "A spent rocket body — an upper stage left in orbit after deploying its payload.",
  },
  { pattern: /\bDEB\b/i, blurb: "Tracked orbital debris." },
];

export function getSatelliteBlurb(name) {
  if (Object.prototype.hasOwnProperty.call(KNOWN_BLURBS, name)) {
    return KNOWN_BLURBS[name];
  }
  const match = PATTERN_BLURBS.find(({ pattern }) => pattern.test(name));
  return match ? match.blurb : null;
}
