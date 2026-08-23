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

  // Notable science/astronomy/Earth-observation satellites — same
  // confidence bar as the station/crew entries above: only ones with a
  // well-documented, verifiable mission, not a guess from the name alone.
  TERRA:
    "NASA's flagship Earth-observing satellite, launched in 1999 — its five instruments (including MODIS) monitor clouds, land, and oceans.",
  AQUA:
    "A NASA Earth-observing satellite studying the planet's water cycle: clouds, precipitation, ice, and oceans.",
  HST: "The Hubble Space Telescope — in orbit since 1990, one of the most productive scientific instruments ever built.",
  "SEASAT 1":
    "NASA's first satellite dedicated to radar ocean remote sensing — hugely influential despite failing after just 105 days in 1978.",
  ENVISAT:
    "The European Space Agency's Envisat — one of the largest civilian Earth-observation satellites ever flown, lost contact unexpectedly in 2012.",
  "ERS-1":
    "The European Space Agency's first Earth Remote Sensing satellite, launched in 1991.",
  "ALOS (DAICHI)":
    "A Japanese (JAXA) Earth-observation satellite for mapping, disaster monitoring, and resource surveying.",
  "ALOS-2": "A Japanese (JAXA) radar imaging satellite, successor to ALOS (Daichi).",
  "AJISAI (EGS)":
    "A Japanese geodetic satellite — a mirrored sphere used for laser ranging to precisely measure Earth's shape, not an active spacecraft.",
  "ASTRO-H (HITOMI)":
    "A JAXA X-ray astronomy satellite lost in 2016 after an attitude-control failure, just over a month after launch.",
  XRISM:
    "A JAXA/NASA X-ray astronomy satellite, launched as a successor mission after Hitomi's loss.",
  "HXMT (HUIYAN)":
    "China's first X-ray astronomy satellite (Hard X-ray Modulation Telescope), also known as Insight.",
  "OAO 2":
    "One of NASA's Orbiting Astronomical Observatories — early space telescopes from the late 1960s/70s that helped pave the way for Hubble.",
  "OAO 3 (COPERNICUS)":
    "One of NASA's Orbiting Astronomical Observatories — early space telescopes from the 1970s that helped pave the way for Hubble.",
  "SERT 2":
    "A NASA technology-demonstration satellite from 1970 — one of the first long-duration tests of ion-engine propulsion in orbit.",
  "ISIS 1":
    "A Canadian ionospheric-research satellite from 1969 — Canada was one of the first countries besides the US and USSR to build its own satellite.",
  "MIDORI II (ADEOS-II)":
    "A Japanese (JAXA) Earth-observation satellite lost in 2003 after a power system failure.",
  "COSMO-SKYMED 1":
    "An Italian radar Earth-observation satellite, part of a four-satellite constellation for both civilian and military use.",
  "SAOCOM 1A": "An Argentine radar Earth-observation satellite.",
  "SAOCOM 1B": "An Argentine radar Earth-observation satellite.",
  "ORBVIEW 2 (SEASTAR)":
    "Carries NASA's SeaWiFS instrument, which monitored ocean color and plant life from 1997 to 2010.",
  ACS3:
    "NASA's Advanced Composite Solar Sail System — a 2024 technology demonstration of a lightweight solar sail boom design.",
  "KORONAS-FOTON":
    "A Russian satellite studying the Sun, part of the KORONAS solar-research program.",
  "HELIOS 1B": "A French military optical reconnaissance satellite.",
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
  {
    // "Cosmos" has covered a huge range of Soviet/Russian missions since
    // 1962 without disclosing which — stating that fact honestly beats
    // guessing at any individual satellite's actual purpose.
    pattern: /^COSMOS \d/i,
    blurb:
      "\"Cosmos\" is a generic designation Russia (and the USSR before it) has used since 1962 for a huge range of satellites — military, scientific, and technology tests — often without disclosing the specific purpose.",
  },
  {
    // Same idea as Cosmos, US side: "USA-###" is a deliberately generic
    // cover name, not a mission title.
    pattern: /^USA \d/i,
    blurb:
      "\"USA\" followed by a number is a generic cover designation the U.S. government uses for military and intelligence satellites, whose specific mission is often not publicly disclosed.",
  },
  {
    pattern: /^SPACEMOBILE-\d/i,
    blurb:
      "An AST SpaceMobile satellite — part of a commercial constellation aiming to connect ordinary cellphones directly to satellites; their huge antenna arrays make them some of the brightest objects in the night sky.",
  },
  {
    // China's official line is civilian remote sensing; several Yaogan
    // satellites are widely reported by independent space analysts as
    // serving military reconnaissance roles too — stated as reported,
    // not asserted as confirmed fact either way.
    pattern: /^YAOGAN-\d/i,
    blurb:
      "Part of China's Yaogan remote-sensing satellite series — officially for Earth observation, though independent analysts have widely reported some as serving military reconnaissance roles too.",
  },
];

export function getSatelliteBlurb(name) {
  if (Object.prototype.hasOwnProperty.call(KNOWN_BLURBS, name)) {
    return KNOWN_BLURBS[name];
  }
  const match = PATTERN_BLURBS.find(({ pattern }) => pattern.test(name));
  return match ? match.blurb : null;
}

// DESIGN.md backlog item 14: docked/attached-object handling, frontend
// half (the alert side was already correctly scoped to ISS-only from day
// one — nothing to change there). Same confidence bar as KNOWN_BLURBS
// above: only pairs we can actually verify, not "everything CelesTrak's
// stations group happens to include" — that group also carries genuinely
// independent small satellites and debris (e.g. deployed-from-ISS
// cubesats) that just share a similar orbit, not a docked one, and
// tagging those would be flatly wrong. Manually maintained — real docking
// status changes over weeks/months as vehicles arrive and depart, so this
// will drift and needs occasional review, same as the blurbs.
const HOST_STATION = {
  "ISS (NAUKA)": "ISS (ZARYA)",
  POISK: "ISS (ZARYA)",
  "CSS (WENTIAN)": "CSS (TIANHE)",
  "CSS (MENGTIAN)": "CSS (TIANHE)",
  "CYGNUS NG-24": "ISS (ZARYA)",
  "CREW DRAGON 12": "ISS (ZARYA)",
  "PROGRESS-MS 33": "ISS (ZARYA)",
  "PROGRESS-MS 34": "ISS (ZARYA)",
  "SOYUZ-MS 28": "ISS (ZARYA)",
  "SHENZHOU-23 (SZ-23)": "CSS (TIANHE)",
  "TIANZHOU-10": "CSS (TIANHE)",
};

// Derived, not hand-maintained separately — one source of truth (the map
// above) backs both the "what am I docked to" and "what's docked to me"
// directions, so they can't drift out of sync with each other even though
// the underlying facts still need periodic human review.
const DOCKED_AT = {};
for (const [name, host] of Object.entries(HOST_STATION)) {
  (DOCKED_AT[host] ??= []).push(name);
}

export function getHostStation(name) {
  return HOST_STATION[name] || null;
}

export function getDockedObjects(name) {
  return DOCKED_AT[name] || [];
}
