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
| Git / GitHub | Version control (repo: MacGotHub/satellite-tracker — **not yet created**) |

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
├── opentofu/                  # All IaC (single root module)
│   ├── backend.tf             # S3 remote state (351668480009-opentofu-state, key sattrack/tle-pipeline) ✓
│   ├── providers.tf           # AWS ~>5.0 + archive providers, us-east-1 ✓
│   ├── main.tf                # Phase 1 as-built: DynamoDB, S3 archive, Lambda, IAM, Scheduler ✓
│   ├── locals.tf              # Naming prefix, common tags, API route map ✓
│   ├── outputs.tf             # Table/bucket/function names, API endpoint — grows per phase ✓
│   ├── lambda_api.tf          # Phase 2 — position API Lambda + Skyfield layer ✓
│   ├── api_gateway.tf         # Phase 2 — HTTP API + routes ✓
│   ├── variables.tf           # cesium_ion_token (supplied via gitignored *.auto.tfvars) ✓
│   ├── frontend.tf            # Phase 3 — S3 + CloudFront (OAC) + generated config.js ✓
│   ├── alerts.tf              # Phase 4 — pass-check Lambda + SNS topic + SMS subscription (TODO)
│   └── cicd_oidc.tf           # Phase 5 — GitHub OIDC provider ref + scoped deploy role (TODO)
├── src/                       # Lambda source (Python)
│   ├── tle_fetch/             # Phase 1 handler ✓
│   ├── api/                   # Phase 2 handler — 4 routes ✓
│   ├── shared/                # Pass/visibility logic shared by API + Phase 4 alerts ✓
│   ├── alerts/                # Phase 4 handler (TODO)
│   └── layers/skyfield/       # Layer build.py + requirements (zip output gitignored) ✓
├── tests/                     # pytest + moto unit tests (Phase 1 covered) ✓
├── frontend/                  # Phase 3 — CesiumJS globe (index.html, app.js, app.css) ✓
│                              #   config.js is generated at deploy time, not in repo
└── .github/
    └── workflows/             # Phase 5 — plan/apply pipelines (TODO)
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

### In Progress / TODO
- **Phase 3:** CesiumJS globe, S3 + CloudFront hosting
- **Phase 4:** Pass-check Lambda, SNS topic + SMS subscription, dedupe flag
- **Phase 5:** GitHub Actions workflows, OIDC provider + scoped deploy role

### Owner Prerequisites (not build tasks)
- Create GitHub repo `MacGotHub/satellite-tracker` (needed before Phase 5)
- ~~Free Cesium ion account + access token~~ — done 2026-07-16, lives in
  gitignored `opentofu/cesium.auto.tfvars`
- Before Phase 5 starts: check whether account 351668480009 already has a
  GitHub OIDC provider from a past lab —
  `aws iam list-open-id-connect-providers` — don't create a duplicate.
  This check happens when Phase 5 starts, not now.

### Known Dependencies
- Phase 2 needs Phase 1's TLE data flowing before positions mean anything
- Phase 3 polls Phase 2's API — API must exist first
- Phase 4 reuses Phase 2's visibility/pass logic
- Build order: Phase 1 → 2 → 3 → 4 → 5 (see DESIGN.md)
