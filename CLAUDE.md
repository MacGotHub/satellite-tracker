# CLAUDE.md — satellite-tracker Project Context

This file provides Claude Code with persistent context about this project,
its owner, goals, and conventions. Read this before making any changes.

---

## Owner

- **Name:** Derek McWilliams
- **Role:** Network Security Engineer (working toward DevSecOps)
- **GitHub:** MacGotHub

---

## Project Purpose

This is Derek's real-time satellite tracker, used for:
1. Actually watching satellites — a 3D globe showing live positions, and an
   SMS alert when a visible ISS pass is coming up over the house so Derek
   and Cam can go outside and watch it
2. Building a DevSecOps portfolio piece — serverless AWS architecture,
   OpenTofu IaC, GitHub Actions CI/CD with OIDC (no static keys), and
   secrets handled the way professional teams handle them
3. Extending the same skill-building style as the sibling `aws-iac-lab`
   project — same owner, same conventions, same "build it like production" bar

Code written here should be enterprise-quality. The "secure" pillar is not
decoration — this project exists in part to prove Derek can do the
DevSecOps job he wants next.

---

## Tooling

| Tool | Purpose |
|---|---|
| OpenTofu | Infrastructure provisioning (all AWS resources) |
| Python 3.x | Lambda functions (TLE fetch, position API, pass alerts) |
| Skyfield | Orbital mechanics — TLE propagation, subpoints, pass/visibility math |
| CesiumJS | 3D globe frontend (static site) |
| GitHub Actions | CI/CD — `tofu plan`/`tofu apply` via OIDC role assumption |
| AWS CLI | Ad-hoc verification and troubleshooting |
| Git / GitHub | Version control (repo: MacGotHub/satellite-tracker, created 2026-07-18) |

**OpenTofu version:** Use whatever is current stable. OpenTofu is the IaC
tool here (not CDK/CloudFormation) for consistency with `aws-iac-lab`.
**AWS Region:** us-east-1 (primary)
**AWS Account ID:** 351668480009 — *assumption*: reuse the same account as
`aws-iac-lab`. Not a hard decision; revisit if Derek wants account isolation
between the lab and this project later.

---

## Repo Structure

```
satellite-tracker/
├── README.md                  # Short pitch + pointers here ✓
├── CLAUDE.md                  # This file ✓
├── DESIGN.md                  # Architecture rationale ✓
├── pytest.ini                 # Test config (pythonpath=., tests/) ✓
├── .checkov.yaml               # Phase 5 — repo-wide accepted-risk skip list (see DESIGN.md) ✓
├── opentofu/                  # All IaC (single root module)
│   ├── backend.tf             # S3 remote state (351668480009-opentofu-state, key sattrack/tle-pipeline) ✓
│   ├── providers.tf           # AWS ~>5.0 + archive providers, us-east-1 ✓
│   ├── main.tf                # Phase 1 as-built: DynamoDB, S3 archive, Lambda, IAM, Scheduler ✓
│   ├── locals.tf              # Naming prefix, common tags, API route map ✓
│   ├── outputs.tf             # Table/bucket/function names, API endpoint — grows per phase ✓
│   ├── lambda_api.tf          # Phase 2 — position API Lambda (Skyfield layer dropped in Phase 6 Step 5) ✓
│   ├── api_gateway.tf         # Phase 2 — HTTP API + routes (1 route as of Phase 6 Step 5) ✓
│   ├── variables.tf           # cesium_ion_token (supplied via gitignored *.auto.tfvars) ✓
│   ├── frontend.tf            # Phase 3 — S3 + CloudFront (OAC) + generated config.js; also Phase 6 Step 4's apigw-satellites cache origin ✓
│   ├── alerts.tf              # Phase 4 — pass-check Lambda + SNS topic + email subscription (SMS pending toll-free registration) ✓
│   └── cicd_oidc.tf           # Phase 5 — GitHub OIDC provider + scoped plan/apply roles ✓
├── src/                       # Lambda source (Python)
│   ├── tle_fetch/             # Phase 1 handler ✓
│   ├── api/                   # Phase 2 handler — 1 route (GET /satellites) as of Phase 6 Step 5 ✓
│   ├── shared/                # Pass/visibility logic — Phase 4 alerts Lambda only as of Phase 6 Step 5 ✓
│   ├── alerts/                # Phase 4 handler ✓
│   └── layers/skyfield/       # Layer build.py + requirements (zip output gitignored) ✓
├── tests/                     # pytest + moto unit tests (Phase 1 covered) ✓
├── frontend/                  # Phase 3 — CesiumJS globe (index.html, app.js, app.css) ✓
│                              #   config.js is generated at deploy time, not in repo
└── .github/
    └── workflows/             # Phase 5 — plan/apply pipelines ✓
```

Do not create TODO directories until their phase actually starts.

**As-built deviations from the original plan** (from the lab8-sattrack
reconstruction — kept because they're deployed and working, not worth
churning):
- Phase 1 lives in a single `main.tf` rather than split
  `dynamodb.tf`/`lambda_tle_fetch.tf` files; split it only if/when a phase
  makes `main.tf` unwieldy.
- `locals.tf` landed with Phase 2 (name prefix, common tags via provider
  `default_tags`, API route map). Phase 1 resources keep their literal
  `sattrack-*` names — renaming would destroy/recreate them. No
  `variables.tf` yet; observer coords arrive as API query params in Phase 2
  and become an SSM input in Phase 4.
- Remote S3 state backend (shared `351668480009-opentofu-state` bucket +
  `opentofu-state-lock` DynamoDB lock table) — better than the implicit
  local-state assumption in the original plan; keep it.

---

## Architecture Overview

Serverless end to end. No EC2, no containers, nothing that bills while idle.

### Data Flow
```
CelesTrak (public TLE source)
    ↓ (EventBridge Scheduler → Python Lambda, periodic fetch)
DynamoDB (satellite catalog: id, name, TLE line1/line2, last-updated)
    ↓ (read on request)
API Gateway (HTTP API) → Python Lambda + Skyfield layer
    ↓ (computes lat/lon/alt subpoints, upcoming passes)
CesiumJS globe (S3 + CloudFront static site, polls the API)

DynamoDB TLEs also feed:
    → Scheduled alerts Lambda → visibility check for home coords
    → SNS topic → SMS to Derek when a good ISS pass is coming
```

### Phases (owner's scoping — preserve these estimates)

| Phase | Scope | Estimate |
|---|---|---|
| 1 — Pipeline | Scheduled TLE fetch Lambda → DynamoDB | ~1 evening (fastest win — familiar AWS/OpenTofu, simple Python) |
| 2 — API | API Gateway + Skyfield Lambda computing live positions and passes | ~1–2 evenings (Skyfield Lambda layer is the fiddly bit) |
| 3 — Globe | CesiumJS frontend on S3 + CloudFront, polls the API | ~1–2 evenings (the fun one — demoable early) |
| 4 — Alerts | Scheduled pass-check Lambda → SNS SMS, with dedupe | ~1 evening (visibility math is the only real thinking; Skyfield mostly handles it) |
| 5 — CI/CD | GitHub Actions + OIDC role, `tofu plan`/`apply` | ~2 evenings, budget patience — the "AccessDenied afternoon." Highest resume value. |

Estimates are Derek's own, evening/weekend pace with Claude Code.

---

## Coding Conventions

### Always follow these patterns:

1. **Same OpenTofu style as `aws-iac-lab`** — `for_each` over repeated
   resource blocks, driven by `locals`; never `count` for keyed collections.

2. **`locals.tf` is the single source of truth** — naming prefixes, common
   tags, the satellite watchlist, schedule expressions. Other files reference
   locals, they don't define their own data.

3. **Common tags on every resource** — always merge `local.common_tags` with
   resource-specific tags using `merge()`.

4. **Least-privilege IAM per Lambda** — each Lambda gets its own execution
   role scoped to exactly the resources it touches (e.g. the TLE fetch role
   can write to the catalog table but cannot publish to SNS).

5. **Secrets never in git** — the alert phone number and home coordinates are
   inputs, not code. Phone number via SSM Parameter Store SecureString /
   Secrets Manager (preferred) or a gitignored `.tfvars` — see DESIGN.md.

6. **Comments explaining the why** — not just what the code does, but why
   design decisions were made (e.g. why the pass logic is shared between the
   API and the alerts Lambda).

7. **Shared visibility logic** — the "upcoming visible passes" computation is
   used by both the Phase 2 API route and the Phase 4 alerts Lambda. Write it
   once (shared module/package in `src/`), don't fork two copies.

---

## What NOT to Do

- Do not hardcode or commit the alert phone number — ever. Same for home
  coordinates; treat observer location as an input variable.
- Do not use static long-lived AWS access keys in GitHub secrets — Phase 5
  is OIDC role assumption, and that's the point of Phase 5.
- Do not switch IaC tools — OpenTofu only, matching `aws-iac-lab`.
- Do not use `count` for keyed multi-resource patterns — use `for_each`.
- Do not vendor Skyfield/numpy into each function zip — they belong in a
  Lambda layer, built once.
- Do not add always-on compute (EC2, Fargate services, provisioned
  concurrency) — cost concern in a personal project; everything is
  scheduled or on-demand.
- Do not create the GitHub repo, Cesium ion token, or OIDC provider on
  Derek's behalf — these are owner prerequisites, tracked below.

---

## Current Status

### Completed
- `README.md`, `CLAUDE.md`, `DESIGN.md` — planning docs (2026-07-09)
- **Phase 1 — deployed 2026-07-10, live in account 351668480009:**
  - DynamoDB table `sattrack` — single-table design, `pk` = NORAD ID,
    `sk` = record type (`"TLE"` now; pass-dedupe items join later)
  - S3 bucket `sattrack-tle-archive-351668480009` — raw TLE response
    archived per fetch (audit/history; not in the original plan, kept)
  - Lambda `sattrack-tle-fetcher` (Python 3.12, stdlib + boto3, no layer)
    fetching the CelesTrak `stations` group (~23 satellites) every 2 hours
    via EventBridge Scheduler
  - Unit tests: `tests/test_tle_fetcher.py` (pytest + moto, 6 tests)
  - History: built as `aws-iac-lab/lab8-sattrack` after a PC crash forked
    the planning docs; consolidated here 2026-07-16 with a verified
    no-change `tofu plan`. Remote state key `sattrack/tle-pipeline`
    was kept as-is.

- **Phase 2 — deployed 2026-07-16, live:**
  - Lambda layer `sattrack-skyfield` (skyfield/numpy/sgp4/jplephem +
    de421.bsp ephemeris at `/opt/data`), built by
    `src/layers/skyfield/build.py` — rerun it if `dist/` is missing (zip is
    gitignored)
  - Lambda `sattrack-api` (read-only DynamoDB) + HTTP API
    `https://acs8sbxe50.execute-api.us-east-1.amazonaws.com` with routes:
    `GET /satellites`, `GET /positions`,
    `GET /satellites/{id}/position`, `GET /satellites/{id}/passes?lat&lon`
  - Shared pass/visibility logic in `src/shared/passes.py` (Phase 4 reuses)
  - Observer coords are query params — never stored server-side in Phase 2
  - Gotcha logged: numpy bools/floats leak into responses unless cast —
    `json.dumps` rejects `numpy.bool_`; covered by an ephemeris-backed test

- **Phase 3 — deployed 2026-07-16, live:**
  - CesiumJS globe (pinned 1.130 from the official CDN, no bundler) on a
    private S3 bucket behind CloudFront with Origin Access Control
  - Polls `GET /positions` every 10 s; click a satellite → panel with
    live coords + browser-geolocation pass prediction (coords go to the
    API as query params only, never stored)
  - `config.js` (API URL + Cesium ion token) is generated by OpenTofu at
    apply time — the token lives in gitignored `cesium.auto.tfvars`
  - Custom CloudFront cache policy with 5-min default TTL — redeploys
    propagate without invalidations
  - Satellite finder: type-ahead search (datalist autopopulated from the
    positions poll) that flies the camera to the match — added after the
    first real-use feedback ("couldn't find the ISS")
  - Verified rendering in-browser by Derek (Cesium ion imagery + moving
    satellites); ion token is machine-local — if it's ever lost, generate
    a new one at cesium.com/ion, it's a 2-minute owner task

- **Phase 4 — deployed 2026-07-18, live:**
  - Lambda `sattrack-alerts` (reuses `shared/passes.py` + the Skyfield
    layer, which now also carries `tzdata` for Eastern-time email copy),
    two EventBridge schedules on one function selected by input payload:
    `imminent` (10-min tick, ~15-min heads-up per visible pass) and
    `digest` (5 PM ET daily, silent unless good passes are coming)
  - Alert bar: pass classified visible AND peak >= 30 deg (locals.tf knob);
    rise/set times still quoted against the 10-deg viewing horizon
  - Dedupe: conditional-put flag items (`sk = ALERT#/DIGEST#<rise>`) in the
    catalog table, claimed BEFORE publish (at-most-once by design); table
    TTL on `expires_at` clears them a week after the pass
  - Observer coords in SSM SecureString `/sattrack/observer` (read at cold
    start, never in repo/state/env); email subscription added out-of-band
    to topic `sattrack-alerts` — SMS joins the topic later, pending
    toll-free origination-number registration (2020s US SNS SMS rule)
  - Verified in-Lambda post-apply: both modes invoke clean; 0 messages was
    cross-checked against the API — all next-72h passes genuinely
    `visible: false` (daylight/shadow), so silence is correct behavior
  - Unit tests: `tests/test_alerts.py` (10 tests; compute_passes stubbed,
    SNS delivery asserted via a moto SQS subscription)

- **Phase 5 — deployed 2026-07-27, live:**
  - `opentofu/cicd_oidc.tf`: GitHub OIDC provider (thumbprint fetched live
    via `data.tls_certificate`, not hardcoded — GitHub rotated its cert in
    2023 and broke everyone who pasted one) + two IAM roles instead of one:
    `sattrack-gha-plan` (repo-scoped, any ref/PR, read-only) and
    `sattrack-gha-apply` (repo **and** `main`-branch scoped, read-write) —
    a PR from any branch can never reach write permissions
  - Gotcha: GitHub's OIDC `sub` claim for this repo comes back as
    `repo:MacGotHub@188585672/satellite-tracker@1305326446:...` — the
    newer immutable-ID format, not a plain `owner/repo` string. Trust
    policies match on the numeric IDs (`local.github_oidc_sub_prefix`),
    which is actually tighter than a name match (survives a rename)
  - `.github/workflows/plan.yml` (PR → read-only plan, output to job
    summary) and `apply.yml` (push to main → `tofu apply -auto-approve`).
    Both pin `tofu_version` and use `set -o pipefail` around
    `tofu ... | tee` — without it, `tee`'s exit code masks a real
    tofu failure and the step shows green
  - `CESIUM_ION_TOKEN` GitHub secret feeds `TF_VAR_cesium_ion_token` — CI
    has no access to the gitignored local `cesium.auto.tfvars`
  - `.gitattributes` (`* text=auto eol=lf`) added — matters for
    `frontend.tf`'s `aws_s3_object` uploads (raw `filemd5()` content
    hashing, so CRLF/LF really would change what gets deployed), though
    it turned out not to be the cause of the Lambda-layer churn below
  - Gotcha (the big one): `src/layers/skyfield/build.py` produced a
    *different zip hash on every single build*, regardless of platform —
    not a permissions issue, a build-determinism one. Two causes, found
    by diffing two builds byte-for-byte: (1) `zf.write()` copied each
    file's real mtime, which is "whenever pip just installed it"; fixed
    via a manually-built `ZipInfo` with a constant `date_time`. (2) pip's
    `python/bin/` console-script launchers (`f2py`, `numpy-config`) embed
    that run's ephemeral `tempfile.mkdtemp()` path, and numpy's
    `RECORD` lists a hash for them too — neither is read at Lambda
    runtime, both excluded. Verified with 3 consecutive local rebuilds
    producing an identical sha256. CI-to-CI reruns now show genuine
    "No changes"; a Windows-local build still differs from CI's Linux
    build by a few hundred KB (real wheel-resolution difference, not a
    determinism bug) — harmless, causes one reconciling replace if
    Derek ever applies locally again, and CI is the primary apply path
    from here anyway
  - Read-only IAM policy scoping took ~10 iterations against real
    `AccessDenied` errors from the AWS provider's own drift-detection
    reads (S3 bucket sub-configs, `logs:DescribeLogGroups` has no
    resource-level support at all, deprecated `ListTagsLogGroup` vs.
    `ListTagsForResource`, etc.) — exactly DESIGN.md's predicted
    "AccessDenied afternoon"
  - First real `apply.yml` run succeeded on every actual AWS change but
    failed to persist state (missing `s3:PutObject` on the state key —
    only `GetObject` + lock-table access had been granted). Real infra
    was briefly ahead of remote state; reconciled locally, permission
    added, `tofu plan` confirmed clean before moving on
  - **Checkov IaC gate — added 2026-07-30** (the one open decision Phase 5
    left unresolved): both `plan.yml` and `apply.yml` now run
    `bridgecrewio/checkov-action` (pinned to `v12.3114.0`, matching the
    `tofu_version` pinning philosophy) against `opentofu/` before anything
    else. Accepted-risk findings live in `.checkov.yaml` (repo-wide) and
    inline `checkov:skip` comments (resource-specific) — see DESIGN.md's
    "Security scanning in CI" section for the full triage and gotchas
    worth knowing before touching either: (1) inline skip comments only
    work *inside* the resource block, not on the line before it; (2)
    enabling this surfaced a real gap, not just scanner noise — HTTP API
    access logging needed a `aws_cloudwatch_log_resource_policy` that
    nothing had required before; (3) the first real `apply.yml` run of
    this batch failed — the CloudFront security-headers policy started as
    a `data "aws_cloudfront_response_headers_policy"` name lookup, but
    OpenTofu reads data sources before applying that same run's IAM
    policy changes, so `gha_apply` AccessDenied'd on a permission it was
    simultaneously being granted. Fixed by hardcoding the (globally
    constant) managed-policy ID as a local instead of looking it up —
    `CKV2_AWS_32` inline-skipped as a result, since that graph check can't
    recognize a literal ID as "attached." Also added while the IAM
    policies were open: AWS-managed-key encryption for DynamoDB/S3/SNS,
    DynamoDB PITR, and a 90-day lifecycle on the TLE archive bucket — all
    free wins with zero blast radius. This round also touched
    `sattrack-gha-read`/`sattrack-gha-write` in `cicd_oidc.tf` to grant
    the new actions these resources need
    (`s3:PutLifecycleConfiguration`, `dynamodb:UpdateContinuousBackups`,
    `logs:PutResourcePolicy`/`DescribeResourcePolicies`, the new
    `/aws/apigateway/sattrack-*` log-group ARN pattern) — same "grow the
    policy in the same PR" discipline as everything else in that file.

- **Phase 6 — client-side propagation, Steps 1-5 deployed 2026-08-01
  through 2026-08-04, live** (see DESIGN.md's Phase 6 section for the full
  as-built rationale and gotchas; this is the condensed status):
  - **Steps 1-2:** satellite.js (v7.1.0, ESM-only CDN import) replaced
    server-computed positions/passes with client-side SGP4 propagation
    against `GET /satellites`'s TLE payload; Geolocation + manual entry
    drive local pass prediction. Parity-verified against the old
    server-Skyfield route before it was retired.
  - **Step 3:** catalog widened to include CelesTrak's `starlink` group
    (~10,769 objects) alongside the 23 tracked stations. `GET /positions`
    retired (no consumers left once Steps 1-2 landed). Bulk swarm renders
    via a separate `PointPrimitiveCollection` (not the Entity API),
    propagation amortized across an `onTick` rolling slice.
  - **Step 4 — deployed 2026-08-04:** `GET /satellites` is now served
    through the existing frontend CloudFront distribution (new
    `apigw-satellites` origin + dedicated `/satellites` cache behavior,
    1-2h TTL matching the 2h TLE fetch cadence) instead of hitting
    Lambda/DynamoDB on every client poll — verified via `X-Cache: Hit
    from cloudfront` on a repeat request. The frontend's generated
    `apiBaseUrl` points at the CloudFront domain, so the call is also
    same-origin now (no CORS preflight).
  - **Step 5 — deployed 2026-08-04:** retired `GET
    /satellites/{id}/position` and `GET /satellites/{id}/passes` (no
    frontend consumers since Steps 1-2) and dropped the Skyfield Lambda
    layer, `shared/passes.py`, and `dynamodb:GetItem` from `sattrack-api`
    entirely — that Lambda is now a pure DynamoDB-Scan-and-serialize
    read of the TLE catalog, no numpy/Skyfield import cost. Skyfield
    stays exactly where Step 5's carve-out says it should: the alerts
    Lambda only, against the fixed home observer. Route-specific tests
    for the two retired routes replaced with 404-on-retired-route
    assertions (mirroring the existing `GET /positions` one); the
    shared-module pass/subpoint tests moved out of `test_api.py` into a
    new `tests/test_passes.py` since they test `shared/passes.py`
    directly and no longer have anything to do with the API handler.
  - **Bug found and fixed during Step 4 verification, not caused by
    it:** the Starlink swarm render could crash with `TypeError: Cannot
    read properties of null (reading 'position')`. Root cause:
    `satellite.propagate()` can return `null` outright for some
    satrecs — not just `{ position: false }`, which was the only case
    the original Step 3 guard checked — plus a quieter case where
    propagation "succeeds" with NaN-filled ECI components that pass a
    truthy check but produce a NaN `Cartesian3` that breaks Cesium's
    bounding-volume math the same way. Reproduced live against the real
    10,769-object swarm and fixed with a shared `hasValidPosition()`
    guard (null + finite-component check); skip logging throttled
    (once per satellite per catalog refresh for the named group, once
    per full cycle for the bulk swarm) so a persistently-bad object
    can't flood the console forever.

- **Post-Phase-6 enhancements** — working through DESIGN.md's backlog
  list now that Phase 6 is closed out. Sequence: pass-window extension →
  widen tracked groups (item 13) → visibility classification (item 8) →
  "next visible pass" hero field (item 9), the last two being how "what's
  visible this week" gets answered.
  - **Item 4 (pass search window) — deployed 2026-08-04, live:** 48h → 14
    days in `findPassesLocal()`. See DESIGN.md item 4 for the full
    as-built note, including a CORS gap found (not fixed) on the raw API
    endpoint for cross-origin callers — harmless for the deployed site
    itself, which is same-origin through CloudFront as of Step 4.
  - **Item 13 (widen tracked groups) — deployed 2026-08-04, live:** added
    CelesTrak's `visual` group (157 objects live, not just ~100 —
    optically brightest, includes bright rocket bodies/debris) to
    `CELESTRAK_GROUP` in `main.tf`. No frontend change needed — the
    existing starlink-vs-everything-else render split already gives
    `visual` full interactive treatment. `alert_watchlist` deliberately
    untouched (still ISS-only) — this is a globe/pass-prediction change,
    not an alerting one. See DESIGN.md item 13 for the group-overlap
    (ISS/Tiangong in both `stations` and `visual`) tag-priority note.
  - **Item 8 (visibility classification) — deployed 2026-08-04, live:**
    new `frontend/sun.js` module — a low-precision solar position formula
    plus a cylindrical Earth-shadow model, since satellite.js has no
    Skyfield-equivalent for either. `findPassesLocal()` passes now carry
    a real `visible: true/false` and a `reason` (`daylight`/`eclipsed`)
    instead of the `null` placeholder. Frontend defaults to visible-only
    with a "show all passes" toggle. Verified correct (not just running)
    by independently scanning sun altitude across 24h and confirming
    sunrise/noon/sunset matched real-world expectations, then
    cross-checking pass classifications against that scan — see
    DESIGN.md item 8 for the full verification writeup and the deliberate
    scope trim (single civil-twilight threshold matching the alerts
    Lambda exactly; no `too_low` category — neither the full
    civil/nautical/astronomical set nor a second stricter threshold
    the original backlog item proposed).
  - **Incident, same night:** shipping `sun.js` (a brand-new file)
    briefly broke the entire live site — the `frontend` bucket's
    `aws:kms` default encryption (set in Phase 5, never actually taken
    up by any *existing* object) applied to this first genuinely new
    key, and CloudFront's OAC can't decrypt SSE-KMS objects on an
    AWS-managed key without a grant that key type can't be given. Fixed:
    `frontend` bucket's default flipped to `AES256` (matches what every
    object already silently used), plus a live re-encrypt of the already
    -uploaded object and a full CloudFront invalidation. `tle_archive`
    unaffected (not served via CloudFront). Full incident writeup in
    DESIGN.md item 8 — worth reading before adding any new file to the
    frontend bucket in the future.
  - **UI layout — deployed 2026-08-20/21, live:** `#observer` (overhead-now)
    and `#panel` (per-satellite detail/passes) moved off a fixed left/right
    split into a shared `#right-stack` flex column, both anchored top-right
    so the two never compete for screen space; `#observer` also gained a
    standalone "Use my location" button (geolocation previously only
    existed indirectly, via the per-satellite passes panel's button).
  - **SATCAT metadata enrichment — deployed 2026-08-21, live:** the bare
    catalog name previously left users to research every satellite
    themselves. `sattrack-tle-fetcher` now also queries CelesTrak's SATCAT
    endpoint (`records.php?GROUP=<group>&FORMAT=json`) per group, same
    per-group loop and 2h cadence as the existing TLE fetch, and merges
    `object_type`/`owner`/`launch_date`/`decay_date`/`rcs` onto the same
    `TLE` item (not a new `sk` — 1:1 with the satellite, changes rarely,
    no reason to pay for a second item or a join on read). Best-effort
    and isolated per group: a SATCAT outage logs and skips enrichment for
    that cycle rather than blocking the TLE write. `OBJECT_TYPE`/`OWNER`
    codes are decoded via `src/tle_fetch/celestrak_codes.py`, transcribed
    from CelesTrak's own published references (satcat-format.php,
    sources.php), falling back to the raw code for anything unrecognized
    rather than guessing. `sattrack-api` serializes the new fields when
    present (not null-padded) and casts `rcs` from DynamoDB's `Decimal` to
    `float`. The frontend detail panel shows them as a new `#panel-facts`
    line above the existing hand-curated `satellite_info.js` blurbs
    (~13 well-known crewed-station spacecraft, added the same session)
    — objective facts for (eventually) the whole catalog including all
    ~10,769 Starlinks, editorial sentence layered on top for the ones
    worth one. Verified live end-to-end: manually invoked
    `sattrack-tle-fetcher` post-deploy rather than waiting for its next
    2h tick, confirmed via `GetItem` (ISS → Payload/International Space
    Station/1998-11-20, an Ariane 40 R/B → Rocket Body/France) and via
    `GET /satellites` through the real CloudFront route the frontend uses.
    Gotcha worth knowing before adding another sibling module to
    `src/tle_fetch/`: that Lambda's `archive_file` used to be a single
    `source_file` (just `handler.py`, flat at the zip root, `handler =
    "handler.handler"`) — adding `celestrak_codes.py` required switching
    to the same multi-`source{}` block pattern `alerts.tf` already uses
    for `handler.py` + `shared/passes.py`, with a matching
    `"tle_fetch.handler.handler"` entrypoint; otherwise the new file
    silently isn't in the deployed zip at all.

### Owner Prerequisites (not build tasks)
- ~~Create GitHub repo `MacGotHub/satellite-tracker`~~ — done 2026-07-18,
  history pushed (was local-only for two days)
- ~~Free Cesium ion account + access token~~ — done 2026-07-16, lives in
  gitignored `opentofu/cesium.auto.tfvars`
- ~~Check for an existing GitHub OIDC provider before Phase 5~~ — done
  2026-07-27, account had none, created directly

### Known Dependencies
- Phase 2 needs Phase 1's TLE data flowing before positions mean anything
- Phase 3 polls Phase 2's API — API must exist first
- Phase 4 reuses Phase 2's visibility/pass logic
- Build order: Phase 1 → 2 → 3 → 4 → 5 (see DESIGN.md)
