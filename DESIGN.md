# DESIGN.md — satellite-tracker Architecture Design Document

**Author:** Derek McWilliams
**Last Updated:** July 2026
**Status:** Phases 1–3 deployed and running (Phases 2–3 on 2026-07-16); Phases 4–5 not started

---

## Purpose

This document describes the architecture and design decisions behind the
satellite-tracker project. It serves as a reference for understanding why
things are built the way they are, and as a guide for building it out
phase by phase.

---

## Background

In the owner's words:

> "I'm building a website that tracks satellites in real time — you'll see
> them moving on a 3D globe, and it'll text me when something cool like the
> Space Station is about to fly over our house so we can go outside and
> watch it. I'm building it the way professional cloud teams do — automated,
> secure, and hosted entirely on Amazon's cloud — so it doubles as a
> portfolio piece for the DevSecOps career move I'm working toward."

Two goals, deliberately entangled:

1. **A real thing Derek will use** — backyard ISS-spotting with Cam.
2. **A DevSecOps proof-of-work** — serverless AWS, OpenTofu IaC, secrets
   handled properly, and CI/CD that authenticates with GitHub OIDC instead
   of static keys. Phase 5 is the single most important "prove you can do
   the job" artifact in the whole project.

This is a sibling to `aws-iac-lab` — same owner, same account, same tooling
preferences (OpenTofu, not CDK/CloudFormation), same enterprise-quality bar.
Where that lab is pure networking, this one is a full application:
data pipeline, API, frontend, alerting, and delivery pipeline.

> **Assumption (not a hard decision):** AWS account 351668480009, us-east-1 —
> the same account as `aws-iac-lab`. Reusing it keeps things simple; if Derek
> later wants isolation between lab experiments and this project, moving to a
> separate account is a revisitable decision, not a foundation crack.

---

## Full Topology

```
                       ┌──────────────────────────────────────────────┐
                       │           AWS Account 351668480009            │
                       │                 us-east-1                     │
                       │                                              │
 CelesTrak             │  ┌────────────────┐      ┌────────────────┐  │
 (public TLE data) ────┼─►│ tle-fetch      │─────►│ DynamoDB        │  │
                       │  │ Lambda (Py)    │      │ satellite       │  │
        EventBridge ───┼─►│                │      │ catalog         │  │
        Scheduler      │  └────────────────┘      └───────┬────────┘  │
        (periodic)     │                                  │           │
                       │                          reads   │           │
                       │  ┌────────────────┐      ┌───────▼────────┐  │
   Browser ────────────┼─►│ API Gateway    │─────►│ position-api    │  │
   (CesiumJS globe,    │  │ (HTTP API)     │      │ Lambda (Py)     │  │
    polls positions)   │  └────────────────┘      │ + Skyfield layer│  │
        ▲              │                          └───────┬────────┘  │
        │              │  ┌────────────────┐              │ shared    │
        └──────────────┼──│ CloudFront     │              │ pass/     │
                       │  │   └─ S3 bucket │              │ visibility│
                       │  │ (static site)  │              │ logic     │
                       │  └────────────────┘      ┌───────▼────────┐  │
                       │                          │ pass-alert      │  │
        EventBridge ───┼─────────────────────────►│ Lambda (Py)     │  │
        Scheduler      │                          └───────┬────────┘  │
                       │                                  │           │
                       │                          ┌───────▼────────┐  │
                       │                          │ SNS topic       │──┼──► SMS to Derek
                       │                          │ (SMS sub)       │  │   ("ISS pass at 21:14,
                       │                          └────────────────┘  │     look NW, 60° max")
                       └──────────────────────────────────────────────┘

 GitHub (MacGotHub/satellite-tracker — repo not yet created)
        │  GitHub Actions: tofu plan / tofu apply
        ▼
 GitHub OIDC provider ──► scoped IAM deploy role ──► manages everything above
 (token.actions.githubusercontent.com)
```

Everything is serverless and event-driven — nothing bills while idle, which
matters in a personal account.

---

## Phase 1 — TLE Pipeline (~1 evening)

Fetch TLE (Two-Line Element orbital data) periodically from a public source
(CelesTrak) via a scheduled Python Lambda (EventBridge Scheduler trigger),
store in DynamoDB.

### DynamoDB catalog shape (as built)

| Attribute | Purpose |
|---|---|
| `pk` (partition key) | NORAD catalog ID |
| `sk` (sort key) | Record type — `"TLE"` for catalog entries; leaves room for pass-dedupe items (Phase 4) in the same table |
| `name` | Human-readable name (e.g. "ISS (ZARYA)") |
| `line1` / `line2` | The two TLE lines, stored verbatim |
| `fetched_at` | Timestamp of last successful fetch |

### As-built deltas from the original sketch

Phase 1 shipped (2026-07-10) with three deliberate additions over the
original single-key design:
- **Single-table `pk`/`sk` design** instead of a satellite-only key —
  Phase 4's dedupe flags land in the same table under a different `sk`.
- **S3 raw-TLE archive** (`sattrack-tle-archive-<account>`) — every fetch
  stores the verbatim CelesTrak response for audit/history/replay.
- **Fetches the CelesTrak `stations` group** (~23 satellites, includes the
  ISS) in one request every 2 hours, rather than per-satellite CATNR
  queries — one request per fetch is friendlier to CelesTrak than N.

### Why this design

- **Why fetch on a schedule instead of on demand?** TLEs change slowly
  (hours-to-days freshness is fine), and CelesTrak asks consumers not to
  hammer their endpoints. A periodic Lambda decouples ingest rate from user
  traffic — the API and globe read from DynamoDB, never from CelesTrak.
- **Why DynamoDB?** Tiny key-value dataset, single-digit-millisecond reads,
  on-demand billing rounds to zero at this scale, and no VPC/connection
  management the way RDS would need. It also later hosts the pass-dedupe
  flags (Phase 4) without adding a second store.
- **Why is this Phase 1?** It's Derek's fastest win — mostly familiar
  AWS/OpenTofu; the Python is simple. TLEs flowing into a table makes every
  later phase testable.

---

## Phase 2 — Position API (~1–2 evenings)

API Gateway (HTTP API) + Python Lambda using the Skyfield library to compute
live positions (lat/lon/alt subpoint) from stored TLEs on request.

### Routes

| Route | Purpose |
|---|---|
| List tracked satellites | Catalog for the frontend's satellite picker |
| Current position of one satellite | Single subpoint computation |
| Current positions of all tracked satellites | Bulk endpoint the globe polls |
| Upcoming visible passes for an observer location | Pass prediction — shared logic with Phase 4 |

### The fiddly bit: the Skyfield Lambda layer

Skyfield pulls in heavier dependencies (numpy, jplephem) that don't fit a
casual inline zip. They go in a Lambda layer, built once and attached to both
the API Lambda and the Phase 4 alerts Lambda. This is the one part of Phase 2
Derek has flagged as fiddly — the routes are straightforward once TLEs are
flowing.

### Why compute on request instead of precomputing positions?

A satellite's position changes continuously; precomputing means either stale
data or a tight write loop. Propagating from the TLE at request time gives an
exact answer for "now" with cheap math, and the TLE itself only refreshes on
the Phase 1 schedule.

### Why HTTP API (not REST API)?

Cheaper, simpler, and this project needs none of the REST API extras
(usage plans, request validation models). Matches the "no gold-plating" rule.

---

## Phase 3 — 3D Globe (~1–2 evenings, the fun one)

CesiumJS 3D globe frontend, static site hosted on S3 + CloudFront (keeps the
all-AWS hosting story intact). Polls the Phase 2 bulk-positions endpoint
periodically to animate satellite movement.

### Why CesiumJS?

Purpose-built for exactly this — a 3D WGS84 globe with time-dynamic entities.
It's also very demoable early, so the phase feels fast even when fiddling
with styling.

### Why S3 + CloudFront rather than Amplify/Netlify/etc.?

The hosting story is part of the portfolio pitch: "hosted entirely on
Amazon's cloud," provisioned by the same OpenTofu code as everything else.
S3 + CloudFront is the canonical static-site pattern professional teams use.

### Prerequisite (owner task, not a build task)

A free Cesium ion account + access token is required for default imagery and
terrain. Derek is getting this himself — document it, don't build it. The
token is a frontend config value, not an AWS secret.

---

## Phase 4 — Pass Alerts (~1 evening)

Scheduled Python Lambda checks upcoming passes for a watchlist (at minimum
the ISS) against Derek's home coordinates and publishes to an SNS topic with
an SMS subscription when a good pass is coming up.

### Visibility logic

A pass is worth texting about when all three hold:
1. The satellite is **sunlit** (still catching sunlight at altitude), and
2. The **observer is in darkness** (after dusk / before dawn), and
3. The pass peaks **above some elevation threshold** (too low = trees and
   rooftops).

Skyfield mostly handles this — it's the only real thinking in the phase.
This is the same computation behind the Phase 2 "upcoming visible passes"
route: write it once as shared code, consumed by both Lambdas.

### Dedupe

Without a guard, every scheduler tick before a pass would re-text the same
event. A DynamoDB flag (e.g. an item keyed on satellite + pass window,
written when the alert fires, checked before publishing) makes alerts
idempotent. A TTL on the flag keeps the table from accumulating stale items.

### The phone number is a secret — design requirement, not a style choice

**The alert phone number must never be hardcoded or committed to git.** This
repo is public-portfolio-bound; a personal phone number in `git log` is
forever. Two acceptable mechanisms:

1. **(Preferred)** SSM Parameter Store SecureString (or a Secrets Manager
   secret), created out-of-band, referenced at apply time via a data source.
   Nothing sensitive ever touches the repo, and it's the pattern professional
   teams use — a concrete "how the pros do it" decision Derek can point to in
   the DevSecOps story.
2. A Terraform variable sourced from a gitignored `.tfvars` file — workable,
   but weaker: the secret lives in plaintext on disk and the safety depends
   on `.gitignore` never breaking.

Home coordinates get the same input-not-code treatment (less sensitive than
the phone number, but there's no reason to publish the house's location in a
public repo either).

---

## Phase 5 — CI/CD with GitHub OIDC (~2 evenings, budget patience)

GitHub Actions workflow(s) running `tofu plan` / `tofu apply`, authenticating
to AWS via GitHub's OIDC identity provider and assuming a scoped IAM role —
**not** static long-lived AWS access keys stored as GitHub secrets.

This is the "AccessDenied afternoon": slowest progress per line of code, and
the highest resume value in the project. It's the single most important
"prove you can do the job" artifact here.

### Why OIDC instead of stored keys?

Static keys in GitHub secrets are long-lived credentials that can leak and
must be rotated by hand. With OIDC, GitHub Actions presents a short-lived
signed token, AWS verifies it against the trust policy, and STS issues
temporary credentials for that run only. Nothing long-lived exists to steal.
This is the current professional standard, and doing it correctly — including
the tight trust policy — is exactly the skill a DevSecOps interview probes.

### Trust policy scoping

The IAM role's trust policy must be scoped tightly:
- `aud` condition: `sts.amazonaws.com`
- `sub` condition pinned to **this specific repo** (`MacGotHub/satellite-tracker`)
  and ideally to a specific branch (e.g. `main` for apply)

A trust policy with a wildcard `sub` lets any GitHub repo assume the role —
that's the classic mistake this phase exists to demonstrate avoiding.

### Prerequisites and pre-flight checks (deferred to Phase 5 start, not now)

- **GitHub repo doesn't exist yet.** `MacGotHub/satellite-tracker` must be
  created before this phase. Owner task.
- **Check for an existing OIDC provider first.** Account 351668480009 may
  already have `token.actions.githubusercontent.com` configured from a past
  lab. Run `aws iam list-open-id-connect-providers` before creating one —
  an account can only have one provider per URL, and a duplicate attempt
  fails. If it exists, reference it as a data source instead of creating it.

> **Open decision: security scanning in CI.** Given the "secure" pillar and
> the DevSecOps framing, a static-analysis step on the OpenTofu code in CI
> (e.g. **tfsec** or **checkov**) would strengthen the portfolio story —
> "my pipeline blocks insecure infrastructure before it applies." Options:
>
> 1. **tfsec** — fast, Terraform/OpenTofu-focused, trivially added as an
>    Actions step.
> 2. **checkov** — broader coverage (Terraform plus other frameworks),
>    heavier, larger rule set.
> 3. **Skip for now** — land plan/apply + OIDC first, add scanning as a
>    follow-up commit (which itself demos iterative pipeline hardening).
>
> No commitment yet — decide when Phase 5 is underway. Whichever is chosen,
> the pipeline shape (a scan job gating apply) is the same.

---

## Build Order and Dependencies

The phases are strictly ordered — each one consumes the previous one's output:

```
Phase 1  tle_fetch → DynamoDB      (nothing upstream — fastest win)
        │
        ▼  TLE data must be flowing before positions mean anything
Phase 2  API Gateway + Skyfield Lambda
        │
        ▼  the globe polls the Phase 2 bulk-positions route
Phase 3  CesiumJS globe on S3 + CloudFront
        │
        ▼  alerts reuse Phase 2's shared pass/visibility logic
Phase 4  pass-alert Lambda → SNS → SMS
        │
        ▼  pipeline automates everything already proven by hand
Phase 5  GitHub Actions + OIDC (plan/apply, scoped role)
```

Phase 5 last is deliberate: automating deployment of infrastructure that
already works isolates CI/CD failures to CI/CD — when the pipeline throws
AccessDenied, the infrastructure itself is a known-good quantity.

Owner's estimates (his own, evening/weekend pace with Claude Code):
Phase 1 ~1 evening · Phase 2 ~1–2 evenings · Phase 3 ~1–2 evenings ·
Phase 4 ~1 evening · Phase 5 ~2 evenings.

---

## File Plan

Nothing has been built yet — every row is TODO. Layout keeps OpenTofu in its
own directory (matching `aws-iac-lab`'s structure), Lambda source in `src/`,
the static site in `frontend/`, and workflows in `.github/workflows/`.

| File | Phase | Status | Description |
|---|---|---|---|
| `opentofu/providers.tf` | 1 | TODO | AWS provider, us-east-1 |
| `opentofu/variables.tf` | 1 | TODO | Region, owner, observer coords, phone-number parameter ref |
| `opentofu/locals.tf` | 1 | TODO | Common tags, naming, satellite watchlist, schedules — the brain |
| `opentofu/dynamodb.tf` | 1 | TODO | Satellite catalog table (later also pass-dedupe items + TTL) |
| `opentofu/lambda_tle_fetch.tf` | 1 | TODO | TLE fetch Lambda, IAM role, EventBridge Scheduler |
| `src/tle_fetch/` | 1 | TODO | Python handler — fetch CelesTrak TLEs, upsert to DynamoDB |
| `src/layers/skyfield/` | 2 | TODO | Skyfield/numpy/jplephem layer build (requirements + build script) |
| `opentofu/lambda_api.tf` | 2 | TODO | Position API Lambda + layer attachment, IAM role |
| `opentofu/api_gateway.tf` | 2 | TODO | HTTP API, routes, Lambda integration |
| `src/api/` | 2 | TODO | Python handler — list/position/positions/passes routes |
| `opentofu/frontend.tf` | 3 | TODO | S3 bucket, CloudFront distribution, OAC |
| `frontend/` | 3 | TODO | CesiumJS globe — polls the bulk-positions endpoint |
| `opentofu/alerts.tf` | 4 | TODO | Pass-check Lambda, SNS topic, SMS subscription, scheduler |
| `src/alerts/` | 4 | TODO | Python handler — watchlist passes, visibility check, dedupe, publish |
| `opentofu/cicd_oidc.tf` | 5 | TODO | OIDC provider (create or data-source), scoped deploy role |
| `.github/workflows/` | 5 | TODO | `tofu plan` on PR, `tofu apply` on main; scan step per open decision |
| `opentofu/outputs.tf` | 1+ | TODO | API URL, CloudFront domain, table name — grows per phase |

---

## Cost Posture

Everything here sits in (or near) free tier at hobby scale: on-demand
DynamoDB, scheduled/on-demand Lambda, HTTP API, S3 + CloudFront static
hosting. The only per-use cost worth watching is **SNS SMS**, which bills per
message — the Phase 4 dedupe flag is a correctness feature and a cost
control. No always-on compute anywhere, by design.

---

## Roadmap — Post-Phase-5 Ideas (2026-07-18)

Distilled from a brainstorming session (an external "SATTRACK-README"
doc). That document was written without knowledge of the as-built system —
where it conflicted with what's deployed (different repo, table keys,
routes, schedules), the deployed design wins and the conflict is dropped,
not relitigated. What survives, in rough priority order:

### 1. SATCAT enrichment — "what am I looking at?"

Every object should show owner, object type, and launch info — not just a
cryptic name. Source: **CelesTrak SATCAT** (same provider as the TLEs,
free, no auth, structured CSV/JSON).

- New `satcat-sync` Lambda on a **weekly** schedule (SATCAT changes
  slowly): download, parse, cache as JSON in S3. The Phase 1 `tle_fetch`
  Lambda joins it on NORAD ID at ingest and writes enrichment attributes
  (`owner`, `object_type`, `launch_date`, `launch_site`, `cospar_id`,
  `rcs_size`) onto the existing TLE items — additive attributes only, no
  key-schema change.
- Human-readable `description`, built in priority order: (1) curated
  `src/enrichment/notable_objects.json` (~50 hand-written entries for the
  objects people actually click: ISS, Tiangong, Hubble, GPS, Starlink…);
  (2) deterministic template from SATCAT fields ("Payload owned by China,
  launched 2025-10-31 from Jiuquan").
- **Data-quality rules (hard):** enrich only from authoritative structured
  sources. No scraping third-party tracker sites (documented case of one
  listing a Shenzhou capsule as a SpaceX payload), and no LLM-invented
  descriptions from an object name alone.
- Optional later layer: **GCAT** (planet4589.org, Jonathan McDowell) —
  CC-BY-4.0, TSV downloads, actively maintained; adds owner/manufacturer
  relations and object *phase* data (including "attached to" — see the
  docked-vehicle rule below). Its mission descriptions are sparse, so it
  supplements the curated table rather than replacing it. Attribution
  required: "Data from J. McDowell, planet4589.org".

### 2. Widen the tracked groups (globe, not alerts)

Add CelesTrak groups beyond `stations`: `visual` (the ~100 brightest —
these ARE alert candidates) first; `starlink`, `gnss`, `geo` later for the
globe. Rules learned up front:

- **GNSS/MEO and GEO are globe/data features, never alert candidates** —
  too high and dim for naked-eye passes; GEO renders as a striking
  near-stationary ring.
- Deep-space objects (period > 225 min) trigger SGP4's SDP4 mode; the sgp4
  library under Skyfield switches automatically — no code change.
- Fetch etiquette holds: only pull groups actually in use; the existing
  2-hour cadence is within CelesTrak guidance.
- **Hard boundary:** no TLE exists for beyond-Earth-orbit objects (JWST,
  lunar missions). That's a different data source and math model (JPL
  Horizons/SPICE) — a separate future subsystem, not a config line.

### 3. Docked-vehicle alert dedupe (becomes real when groups widen)

Docked vehicles (Progress/Soyuz/Crew Dragon at the ISS, and NAUKA) carry
near-identical TLEs to `25544`. Alerting per catalog row would fire
multiple "ISS overhead" messages for one flyover. The deployed design
already embodies the fix: `alert_watchlist` in `locals.tf` is an explicit
**allow-list of one primary object per platform** — keep it that way; never
switch the alerter to "everything in a group". GCAT phase data could later
automate the docked-to relationship.

### 4. Smaller candidates

- `GET /overhead?lat&lon` route — "what's above me right now".
- API Gateway throttling (e.g. burst 50 / rate 25) — the API is public;
  pairs well with tightening CORS to the CloudFront domain.
- TTL on TLE items (~7 days) so satellites dropped by CelesTrak age out of
  the catalog instead of lingering with stale orbits (the table's TTL
  attribute already exists for dedupe flags).
- Space-Track.org as a future TLE source upgrade (auth required).
