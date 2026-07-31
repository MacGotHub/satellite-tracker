# DESIGN.md — satellite-tracker Architecture Design Document

**Author:** Derek McWilliams
**Last Updated:** July 2026
**Status:** Phases 1–5 deployed and running (Phases 2–3 on 2026-07-16, Phase 4 on
2026-07-18, Phase 5 on 2026-07-27) — see CLAUDE.md Current Status for the
as-built detail and gotchas from each

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

**Security scanning in CI — resolved 2026-07-30: Checkov.** Landed as a
follow-up commit after plan/apply + OIDC (per option 3 above, as it
happened) — a static-analysis gate in both `plan.yml` and `apply.yml`,
scanning `opentofu/` on every PR and again before every apply (so a direct
push to main can't apply an insecure change unreviewed either). Checkov
over tfsec: broader multi-framework policy-as-code coverage, and the
closer analog to what Harness's IaCM/STO modules integrate — a more
realistic "how a real platform team gates this" answer for the portfolio
story than tfsec's lighter standalone-linter model.

Ran a real scan against this repo before wiring up the gate (55 findings)
rather than turning on a hard-fail blind: most were enterprise controls
that fit a regulated multi-team environment, not a $0-marginal-cost hobby
project (customer-managed KMS CMKs, Lambda-in-VPC, WAF, cross-region
replication, code signing — see "What NOT to Do" / "Cost Posture" above).
Triage split three ways:

1. **Genuinely free fixes, applied**: AWS-managed KMS keys (not customer
   CMKs) for DynamoDB, S3 (both buckets), and SNS — a real step up from
   the invisible default encryption at zero monthly cost; a CloudFront
   managed response-headers policy; API Gateway access logging (which
   turned out to need its own gotcha — see below); DynamoDB
   point-in-time-recovery (negligible cost at ~26 items); an S3 lifecycle
   rule expiring the TLE archive after 90 days.
2. **Accepted-risk, declined by design**: `.checkov.yaml` (repo root) for
   checks that recur across every Lambda/log-group/bucket — VPC, WAF,
   customer CMKs, code signing, DLQ, reserved concurrency, X-Ray, S3
   access-logging/versioning/replication/event-notifications, >1yr log
   retention. Each has an inline rationale in that file. Resource-specific
   exceptions (CloudFront's TLS-min-version/custom-cert/geo-restriction/
   origin-failover, the API's no-auth-by-design) live as `checkov:skip`
   comments next to the resource in `frontend.tf`/`api_gateway.tf`.
3. **Genuinely new engineering, done anyway**: `CKV_AWS_76` (API Gateway
   access logging) surfaced a real gap, not just a scanner nit — HTTP
   APIs (apigatewayv2) deliver access logs via a CloudWatch Logs
   *resource policy* on the destination log group, unlike REST APIs'
   account-level "CloudWatch role ARN" setting. Missing it means the
   stage update silently never delivers logs. Added
   `aws_cloudwatch_log_resource_policy` in `api_gateway.tf` with an
   explicit `depends_on` so it's created before the stage references it.

**Gotcha:** Checkov's inline `# checkov:skip=ID:reason` suppression only
works if the comment sits *inside* the resource block (any line strictly
between its opening and closing brace) — placing it on the line
immediately *before* the resource, which is how most examples online show
it, is silently ignored. Confirmed by reading
`checkov/terraform/context_parsers/base_parser.py`'s
`_collect_skip_comments`: it only attaches a skip comment to a resource
when `start_line < skip_check_line_num < end_line` (strict inequality on
both ends — the boundary lines themselves don't count either).

**Gotcha:** `CKV_AWS_119` (DynamoDB KMS) and `CKV_AWS_26`/`CKV_AWS_145`
(SNS/S3 KMS) look like the same "add a KMS key" ask but aren't — the
SNS/S3 checks are satisfied by *any* `kms_master_key_id`/`aws:kms`
setting, including the free AWS-managed key alias. `CKV_AWS_119`'s Python
check (`DynamoDBTablesEncrypted`) literally requires a populated
`kms_key_arn` — by definition a customer-managed key, since
`alias/aws/dynamodb` is exactly the default it's checking you moved away
from. No free path exists for that one; it's in the accepted-risk skip
list with the real (if lesser) improvement — AWS-managed-key
`server_side_encryption` — still applied in `main.tf`.

**Gotcha (the real one this round — first `apply.yml` run failed on it):**
the CloudFront security-headers policy started as
`data "aws_cloudfront_response_headers_policy" "security_headers" { name =
"Managed-SecurityHeadersPolicy" }`, looked up at plan/apply time. Worked
fine locally (Derek's own AWS user already has broad permissions) but
failed in CI: `gha_apply`'s AccessDenied on
`cloudfront:ListResponseHeadersPolicies` — because OpenTofu reads data
sources *before* applying that same run's resource changes, so a first-
ever apply granting the read permission can't use it until a second run.
Fixed by hardcoding the policy ID directly as a local
(`cloudfront_managed_security_headers_policy_id` in `locals.tf`) instead
of looking it up — these AWS-managed policy IDs are global constants,
identical in every account, so a live lookup was never actually buying
anything. Traded one Checkov finding for it: `CKV2_AWS_32` is a graph
check that only recognizes a connection to a
`response_headers_policy`/`data.aws_cloudfront_response_headers_policy`
resource, so a literal ID (functionally identical) is invisible to it —
inline-skipped on `aws_cloudfront_distribution.frontend` with the full
reasoning in the comment. General lesson: a data source that only a
*new* permission grant can satisfy is never safe inside the same apply
that grants it — hardcode the value if it's a stable constant, or split
into two applies if it isn't.

**Gotcha (second apply failure, same batch):** fixing the above wasn't
the whole story — the *next* run failed with three more AccessDenied
errors (`logs:CreateLogGroup` on the new `/aws/apigateway/*` pattern,
`dynamodb:UpdateContinuousBackups`, `s3:PutLifecycleConfiguration`), the
identical class of bug: new actions added to `gha_write` in the same
apply that uses them, and OpenTofu doesn't serialize otherwise-unrelated
resource applies. The obvious fix — `depends_on = [aws_iam_policy.gha_write]`
on the resources needing the new actions — hit a dependency **cycle**:
`gha_write` is one Terraform resource, so it has an edge to *every*
resource any of its statements reference, across the whole policy, not
just the statement relevant to a given action. `aws_dynamodb_table.sattrack`
already flows into `gha_write` (its `DynamoDbWrite` statement's `Resource`
was `aws_dynamodb_table.sattrack.arn`) — and it turns out `gha_write` also
flows back to the table transitively through `aws_lambda_function.tle_fetcher`
(env var `TABLE_NAME = aws_dynamodb_table.sattrack.name`) and
`aws_scheduler_schedule.tle_fetcher`. Fix: don't try to depends_on the
big cross-referenced policy at all — put the handful of brand-new actions
in a separate `aws_iam_policy.gha_write_bootstrap` (see `cicd_oidc.tf`)
with every `Resource` hand-built as a string, zero attribute references,
so it has no edges into the rest of the graph — then the dependent
resources safely `depends_on` *that* attachment instead. Lesson underneath
the lesson: in a policy document built from `for_each`/reference-heavy
locals, "add a new permission for a new resource" and "let that new
resource depend on the policy" are not automatically compatible — check
what else the policy already references before wiring up depends_on.

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

## Roadmap — Post-Phase-5 Ideas (updated 2026-07-21)

Two source documents feed this roadmap:

- An external "SATTRACK-README" brainstorm doc (distilled 2026-07-18) —
  written without knowledge of the as-built system; where it conflicted
  with what's deployed (different repo, table keys, routes, schedules),
  the deployed design won and the conflict was dropped, not relitigated.
- `DESIGN-addition-observer-and-pass-geometry.md` (merged 2026-07-21) —
  proposed against the deployed Phases 1–4 design (4 routes,
  `pk=<norad_id>`/`sk="TLE"`, observer in SSM SecureString
  `/sattrack/observer`, alert bar of visible + peak ≥ 30°). Sequenced
  after Phase 5 so these ship through the new CI/CD pipeline as its first
  real change.

Ordered cheapest/easiest → hardest. Everything below is $0 marginal AWS
cost unless noted — no item requires a table-schema change.

### Quick wins (trivial–easy, no new infra)

1. **TTL on TLE items** (~7 days) — the table's TTL attribute already
   exists for the Phase 4 dedupe flags; reuse it so satellites dropped by
   CelesTrak age out of the catalog instead of lingering with stale
   orbits.
2. **API Gateway throttling** (e.g. burst 50 / rate 25) — the API is
   public; pairs well with tightening CORS to the CloudFront domain.
3. **Pass geometry — return the arc, not the apex.** Skyfield's
   `find_events` already yields three events per pass (rise/culminate/set);
   the current implementation keeps only culmination. Compute altitude and
   azimuth at all three and return them, additive to the existing
   response:

   ```
   {
     "rise":      { "utc": "...", "az": 315.2, "compass": "NW" },
     "peak":      { "utc": "...", "az":  22.4, "compass": "NNE", "alt": 36.1 },
     "set":       { "utc": "...", "az":  91.7, "compass": "E"  },
     "duration_s": 412,
     "visibility": "visible",
     "reason": null
   }
   ```

   `compass` is a derived 16-point label from azimuth, computed
   server-side so the frontend and any future alert text agree on one
   rendering. `duration_s` matters more than it looks — it's the
   difference between "glance up" and "there's time to get outside."
   Purely derived from TLEs already in DynamoDB.
4. **Extend the pass search window from 48h to 10–14 days.** ISS-class
   visible passes arrive in clusters separated by weeks, so a short window
   legitimately returns zero most of the time. Propagating one satellite
   over two weeks is milliseconds — cost is negligible.
5. **Observer location as a query parameter** —
   `GET /satellites/{id}/passes?lat=&lon=&elev=`, falling back to the SSM
   observer when omitted. Keeps the existing alerting path and any current
   callers working untouched. **Privacy decision:** round coordinates to 2
   decimal places (~1 km) before they enter the query string — more than
   sufficient for pass prediction, and it materially reduces what lands in
   CloudFront/API Gateway access logs. Document this reasoning inline;
   it's the system's answer to "how did you handle location PII?"
6. **UTC-only API, browser-side localization.** The API returns UTC
   ISO-8601 exclusively; the browser localizes via `Intl.DateTimeFormat`.
   The Lambda never guesses a timezone. The 5 PM ET digest schedule itself
   stays ET — that's owner-specific, not user-facing.
7. **Frontend observer input, easy half** — browser Geolocation API (one
   permission prompt) and manual lat/lon entry. Persist the selection in
   `localStorage`; display the active observer so users know what they're
   looking at.

### Medium (real new logic, one new component, or infra widening)

8. **Visibility — classify, don't filter.** Return every pass in the
   window with an explicit verdict instead of silently dropping
   non-visible ones: `visible` (satellite sunlit, observer sun altitude
   below the twilight threshold, peak ≥ threshold), `daylight` (observer's
   sky too bright), `eclipsed` (satellite in Earth's shadow), `too_low`
   (geometry fine, peak below threshold). This replaces the current
   boolean "is it night" check with the actual sun altitude at the
   observer (civil/nautical/astronomical twilight thresholds) — above
   ~50°N in summer the sun never drops far enough for true darkness, and a
   naive night flag fails in one direction or the other. Correct
   thresholds behave properly from Miami to Yellowknife. Frontend defaults
   to visible-only with a "show all passes" toggle — the verdict makes the
   feature read as *explaining the sky* rather than returning nothing.
9. **"Next visible pass" hero field** at the top of the panel, once #8
   lands — this is the question users are actually asking. Handle the
   honest empty case explicitly: no visible passes in the next N days,
   with a one-line explanation that visibility comes in clusters.
10. **Sky-arc UI component.** Render each pass as a small chart: azimuth
    along the horizon axis (compass labels), elevation on the vertical
    axis, an arc from rise through peak to set with times marked. Replaces
    reading three numbers with a single glance. Worth surfacing alongside
    it: an elevation reference (a fist at arm's length ≈ 10°, so 36° ≈ 3.5
    fists up) and direction of travel — users scan a path, not a point.
11. **Click-to-drop observer pin on the Cesium globe** — natural in
    Cesium, and the strongest demo moment of the observer-location work.
    Third of the four frontend input paths (after geolocation and manual
    entry, before city search).
12. **Inclination-limit display.** An observer poleward of a satellite's
    inclination never gets a high pass (ISS at 51.6°, Tiangong at ~41.5°).
    No filtering change needed — showing compass bearing and peak
    elevation (already true once #3 lands) makes a permanently-low pass
    legible instead of looking like a bug.
13. **Widen the tracked groups (globe, not alerts)** — add CelesTrak
    `visual` group (~100 brightest; these ARE alert candidates) first;
    `starlink`/`gnss`/`geo` later for the globe only. Rules learned up
    front: GNSS/MEO and GEO are globe/data features, never alert
    candidates (too high and dim for naked-eye passes; GEO renders as a
    striking near-stationary ring); deep-space objects (period > 225 min)
    trigger SGP4's SDP4 mode automatically — no code change; fetch
    etiquette holds, only pull groups actually in use, the existing 2-hour
    cadence stays within CelesTrak guidance. **Hard boundary:** no TLE
    exists for beyond-Earth-orbit objects (JWST, lunar missions) — that's
    a different data source and math model (JPL Horizons/SPICE), a
    separate future subsystem, not a config line. **Cost note:** stop at
    `visual` for a while — `starlink` alone is thousands of objects and is
    the one item on this list that could actually move the DynamoDB
    storage/read needle, unlike everything else here.
14. **Docked-object handling**, both layers together once #13 lands.
    Alert-side dedupe is already shipped — `alert_watchlist` in
    `locals.tf` is an explicit allow-list of one primary object per
    platform (Progress/Soyuz/Crew Dragon and NAUKA carry near-identical
    TLEs to `25544`; alerting per catalog row would fire multiple "ISS
    overhead" messages for one flyover). Keep it an allow-list; never
    switch the alerter to "everything in a group." The frontend layer is
    new: flag docked objects (e.g. a Tianzhou cargo vehicle attached to
    Tiangong shares the station's orbit, and its apparent brightness is
    the station's, not the cargo craft's) so two catalog entries tracing
    the same arc doesn't read as a bug. GCAT phase data (see #16) could
    later automate the docked-to relationship.
15. **`GET /overhead?lat&lon` route** — "what's above me right now."

### Harder (external dependencies or multi-part builds)

16. **SATCAT enrichment — "what am I looking at?"** Every object should
    show owner, object type, and launch info, not just a cryptic name.
    Source: **CelesTrak SATCAT** (same provider as the TLEs, free, no
    auth, structured CSV/JSON).

    - New `satcat-sync` Lambda on a **weekly** schedule (SATCAT changes
      slowly): download, parse, cache as JSON in S3. The Phase 1
      `tle_fetch` Lambda joins it on NORAD ID at ingest and writes
      enrichment attributes (`owner`, `object_type`, `launch_date`,
      `launch_site`, `cospar_id`, `rcs_size`) onto the existing TLE items —
      additive attributes only, no key-schema change.
    - Human-readable `description`, built in priority order: (1) curated
      `src/enrichment/notable_objects.json` (~50 hand-written entries for
      the objects people actually click: ISS, Tiangong, Hubble, GPS,
      Starlink…); (2) deterministic template from SATCAT fields ("Payload
      owned by China, launched 2025-10-31 from Jiuquan").
    - **Data-quality rules (hard):** enrich only from authoritative
      structured sources. No scraping third-party tracker sites
      (documented case of one listing a Shenzhou capsule as a SpaceX
      payload), and no LLM-invented descriptions from an object name
      alone.
    - Optional later layer: **GCAT** (planet4589.org, Jonathan McDowell) —
      CC-BY-4.0, TSV downloads, actively maintained; adds
      owner/manufacturer relations and object *phase* data (including
      "attached to," feeding #14's automation). Its mission descriptions
      are sparse, so it supplements the curated table rather than
      replacing it. Attribution required: "Data from J. McDowell,
      planet4589.org".
17. **City search geocoder** — last of the four frontend observer-input
    paths, deliberately sequenced last because it needs a geocoder
    dependency. **Cost/privacy flag:** unlike everything else on this
    list, this hands a typed location string to a third-party service and
    may carry real per-request cost depending on provider (Nominatim is
    free but rate-limited; Google/Mapbox-class geocoders charge). Decide
    the provider deliberately when this item comes up, not by default.
18. **Space-Track.org as a future TLE source upgrade** — requires auth (a
    new secret to manage) and carries migration risk to an already-working
    pipeline; evaluate only if CelesTrak stops being sufficient.

### Out of scope — deliberate

**Alerts stay single-observer.** Multi-user alerting means accounts,
stored locations, per-user SNS subscriptions, and PII custody (PIPEDA/GDPR
exposure for non-US users). That's a Cognito-shaped project with a real
compliance surface, not a feature increment. The *site* serves anyone; the
*alerts* remain Derek's.
