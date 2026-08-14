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

**Gotcha (third apply failure, same batch — the cycle fix wasn't enough
either):** the *exact same three* AccessDenied errors came back on the
next run, even with the cycle-free bootstrap policy and correct
`depends_on` in place. The timestamps in the run log proved the ordering
really was correct this time — `aws_iam_role_policy_attachment.gha_apply_write_bootstrap`
completed at `03:39:56.43`, and the AccessDenied hit about one second
later, at `03:39:57.45`. That's not an ordering bug, it's IAM's eventual
consistency: the attach API call returning success doesn't mean every AWS
authorization backend has the new policy yet, especially for a role that
already has an active assumed-role session mid-apply. Terraform can't see
or wait for that — it only knows the API call returned 200. Fixed with
the standard pattern for exactly this problem: a
`time_sleep.gha_write_bootstrap_propagation` resource (`hashicorp/time`
provider, added to `providers.tf`) with a 10s `create_duration`,
`depends_on` the attachment; the three dependent resources `depends_on`
the sleep instead of the attachment directly. Three-gotcha lesson,
stacked: (1) data sources read before the same apply's own permission
grants land, (2) a cross-referenced IAM policy can't safely be
`depends_on`-ed without checking for cycles through unrelated statements,
(3) even a correctly-ordered IAM change needs a deliberate propagation
gap before anything downstream uses it. All three are one root cause —
granting a permission and consuming it in the same `tofu apply` is
inherently fragile — with three different symptoms depending on exactly
how the consuming resource gets its value (a data source, a resource
attribute reference, or a plain sequential API call).

**Gotcha (fourth apply failure — a genuine deadlock, not just a race):**
`aws iam list-policy-versions` on both `gha_read` and `gha_write` showed
exactly 5/5 — IAM's hard cap on customer-managed policy versions,
reached simply from the number of times this session updated them.
Past the cap, an update must delete an old version before creating the
new one, which needs `iam:ListPolicyVersions` — an action neither policy
granted itself. This one is unfixable by CI alone: `gha_apply` can't grant
itself a permission via an update that itself requires that permission to
execute. Required a one-time intervention from credentials that already
had `iam:ListPolicyVersions` (an admin AWS CLI session) to delete one old
version from each policy, freeing a slot so the *next* `CreatePolicyVersion`
call succeeds outright — bypassing the need to list/prune this once. That
next version permanently includes `iam:ListPolicyVersions`, so every
future cap-hit self-heals through CI without help.

**Gotcha (fifth apply failure — the read-side mirror of gotcha #3):**
even with the version cap cleared, the API Gateway log group's post-create
tag read (`logs:ListTagsForResource`) failed — `gha_read`'s own update
(to cover the new `/aws/apigateway/*` pattern) hadn't even *started*
before the read happened, confirmed by its absence from the apply log
entirely (only a "Refreshing state" line, no "Modifying..."). Same root
cause as the write-side bootstrap problem, just discovered later because
the write-side race had been masking it — you can't observe a read-side
ordering bug on a resource that never finishes creating. Fixed with the
mirror-image solution: a `gha_read_bootstrap` policy (the new
`/aws/apigateway/*` tag-read pattern, plus `logs:DescribeResourcePolicies`)
attached to both roles, gated by its own `time_sleep`, with the log group
and log resource policy `depends_on` both sleeps (read and write). Net
lesson across all five: when a change adds both new resources *and* new
permissions for those resources in the same apply, budget for a
bootstrap-policy-plus-sleep on **both** the write side and the read side
— the read side's failure mode is just quieter (a stuck/tainted resource,
not a loud config error) so it surfaces one round later.

**Gotcha (sixth apply failure — refresh happens outside the dependency
graph entirely):** the read-bootstrap fix above still didn't clear the
log group error, and this time the apply log showed *no* "Modifying..."
line for anything at all before the failure — it happened during
OpenTofu's state **refresh** step, which runs before the plan/apply graph
is even built. A resource already tracked in state (the log group,
tainted from an earlier partial failure but still real in AWS) gets its
current attributes — including tags — read back on every single
plan/apply, unconditionally, regardless of any `depends_on` wiring.
`depends_on` only orders the *apply* graph; it has no effect on refresh.
Fixed by deleting the actual AWS log group directly (`aws logs
delete-log-group`) so the next run's refresh finds nothing to read (a
clean "gone from state," not an authorization failure) and creates it
fresh instead — a genuine create *does* respect `depends_on`. General
rule: a stuck/tainted resource that requires a not-yet-granted
permission just to refresh can't be un-stuck by any Terraform-side
dependency fix — the underlying resource has to be removed out-of-band
first.

**Gotcha (same run, once refresh stopped being the blocker): two
permissions were still missing outright, not just racing.** (1)
`gha_write` had *never* actually received the `iam:ListPolicyVersions`
grant from the fourth-failure fix — every prior run failed before ever
reaching gha_write's own update in the graph, so the fix sat correct in
git the whole time without ever reaching AWS. Broken by manually
creating a new policy version via the AWS CLI (using admin credentials,
outside Terraform) with exactly the one action added — the same
"someone with more permission than the CI role has to make the first
move" pattern as gotcha #4, just discovered a run later because the
refresh bug upstream of it had been masking it too. (2) API Gateway v2
access logging turned out to need a wholly separate permission surface —
`logs:CreateLogDelivery`/`GetLogDelivery`/`UpdateLogDelivery`/
`DeleteLogDelivery`/`ListLogDeliveries` — distinct from plain log-group
writes; AWS's CloudWatch "log delivery" objects have their own opaque
IDs with nothing meaningful to scope `Resource` to, so `"*"` is the
correct scope here, not a shortcut. Added to `gha_write_bootstrap`
alongside the existing log-group actions.

Running tally: getting one CI-managed IaC security gate through its
*first* real apply took six failed runs across four distinct root causes
(a data-source read racing its own permission grant, a policy so
cross-referenced that `depends_on` created a cycle, IAM's eventual
consistency after a correctly-ordered grant, and two flavors of "the fix
never actually reached AWS because something upstream failed first").
None of these would show up in `tofu plan` locally with admin
credentials — they are specifically CI-scoped-role problems, which is
exactly why Phase 5 flagged this as the "AccessDenied afternoon" before
a single line of Phase 5 code existed.

**Gotcha (seventh apply failure — the sleep only sleeps once):** the
`logs:CreateLogDelivery` fix from gotcha #6 still failed on its next
run. `time_sleep`'s `create_duration` only elapses when the resource
itself is *created* — `depends_on` an attachment resource only orders
the wait relative to that attachment's first-ever creation, not any
later change to the *policy's own content*. Since `gha_write_bootstrap`
was updated in place (new `LogsDeliveryWrite` statement, same attachment,
same ARN), nothing told the already-existing `time_sleep` to wait again.
Fixed by adding a `triggers = { policy = aws_iam_policy.*.policy }` block
to both bootstrap sleeps — changing either policy's body now forces that
`time_sleep` to be replaced (and re-wait), not just reordered. Lesson:
`depends_on` alone is a one-time bootstrap fix; a policy that keeps
changing across an iterative debugging session needs its propagation
gate tied to the policy's *content*, not just its existence.

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

## Roadmap — Post-Phase-5 Ideas (updated 2026-07-31)

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
- `DESIGN-addition-phase6-client-side-propagation.md` (merged 2026-07-31)
  — an architectural shift, not an incremental feature, so it's called
  out as its own subsection below rather than slotted into the numbered
  cheapest→hardest list. Reviewed against the actual code (not just the
  proposal) before merging: checked `alerts/handler.py` to confirm it
  never calls the HTTP API (reads DynamoDB + `shared/passes.py` directly),
  which is what let the route-retirement decision be concrete instead of
  speculative.

Ordered cheapest/easiest → hardest. Everything below is $0 marginal AWS
cost unless noted — no item requires a table-schema change.

### Phase 6 — client-side propagation (architectural shift, proposed)

**The idea:** stop computing per-observer positions/passes in the API
Lambda. TLEs are observer-independent — the same elements work for any
viewer, anywhere — so the server's job shrinks to "distribute TLEs," and
each browser runs SGP4 itself (via **satellite.js**) combined with its
*own* location (Geolocation API or manual entry) to get positions,
look-angles, and passes on-device. One shift, three payoffs:

1. **Arbitrary observer locations for free** — travel (e.g. a dark-sky
   trip to the NC mountains) or any other viewer just works, no SSM
   rewrite or redeploy, since the observer never leaves the browser.
   Bonus: the traveler's coordinates never land in CloudFront/API Gateway
   access logs, and satellite.js can factor in observer elevation for a
   correctness win exactly where mountain trips need it.
2. **Starlink-scale (~8k objects) becomes a frontend concern, not a
   backend one** — see the caching caveat below for what actually pays
   for this.
3. **Smoother motion** — local interpolation in an animation loop instead
   of a poll-and-snap-to-position cycle.

**Route fate — resolved by checking actual callers, not guessing:**
`alerts/handler.py` never calls the HTTP API at all; it reads DynamoDB
and calls `shared/passes.compute_passes` directly. So `GET /positions`,
`GET /satellites/{id}/position`, and `GET /satellites/{id}/passes` exist
*only* to serve the frontend today — once satellite.js takes over,
nothing server-side consumes any of the three, and they're retired, not
kept as a fallback. `GET /satellites` gets extended with `line1`/`line2`
(and TLE epoch) to become the one payload the client needs. Bonus this
surfaces: with position/pass math gone from `api/handler.py`, the API
Lambda no longer needs the Skyfield layer at all — drops a 32 MB layer
and its cold-start cost, and retires the "numpy bool leaks into
`json.dumps`" bug class for that Lambda for good. Skyfield stays, scoped
to the alerts Lambda only — `shared/passes.py` keeps exactly one caller.

**Carve-out (deliberate): alerts stay server-side.** The SNS pass-alerter
has no browser in the loop — it must compute passes on a schedule in the
backend. Pass math ends up living in two places by design: Skyfield
(Lambda) for SNS alerts against the fixed home observer, satellite.js
(browser) for the interactive site against an arbitrary observer. This
must stay a conscious choice, not drift into an accident.

**Caveat: two SGP4 engines can differ slightly.** Skyfield and
satellite.js implement the same model but are different codebases —
expect ~a second of difference on pass times and small position deltas.
The on-screen arc and the SNS alert text won't match to the millisecond;
that's not a bug. If exact parity ever matters, pick one engine as
authoritative for the displayed number.

**Caveat: caching, not client-side compute, is what actually pays for
Starlink scale.** `_scan_catalog()` in `api/handler.py` is still a
`Scan`, and DynamoDB bills by items scanned, not items returned after
filtering — serving ~8k items costs real read capacity regardless of
where the position math runs. CloudFront caching the TLE payload (TTL
~1–2h, matching Phase 1's fetch cadence — no point serving data fresher
than the source refreshes) is what turns that Scan into a per-cache-miss
cost instead of a per-viewer-per-poll one. This doesn't retire item 13's
caution below: CelesTrak fetch etiquette and DynamoDB storage/scan size
are governed by Phase 1's ingestion schedule and catalog item count, not
by who consumes the result. Phase 6 makes the *frontend* side of
Starlink-scale cheap; the *ingestion* side is item 13's decision to make
deliberately, unchanged by this.

**Interaction with the numbered roadmap below:** items 3, 5, 8, 9, and 15
are written as API/Skyfield enhancements against a route Phase 6 retires
(`/satellites/{id}/passes`) — under Phase 6 they'd be reimplemented
against satellite.js output in the browser instead (or, for 15, possibly
answered client-side from the already-fetched TLE catalog rather than a
new route). Item 7 (Geolocation + manual entry) isn't superseded, it's
just *where* Phase 6's own sequencing step 2 already plans to build it —
same work, one less separate roadmap item to track once Phase 6 starts.
Items 10, 11, 12 are frontend UI/rendering work that stays valid
regardless of which engine produced the numbers. Not resolving each one
here — flagging it so a future pass at any of 3/5/8/9/15 starts by asking
"does Phase 6 change where this lives" instead of building the
server-side version by default.

**Suggested sequencing:**

1. Add satellite.js to the frontend; render one already-tracked group
   client-side from TLEs the API already serves. Verify parity against
   the current server-computed positions.
2. Wire in Geolocation + manual location entry; recompute look-angles
   locally.
3. Add a large group (Starlink) now that count is a client concern; tune
   rendering.
4. Slim/cache the TLE-serving endpoint (CloudFront) and confirm Lambda
   invocation drop.
5. Retire the three superseded routes and the Skyfield layer dependency
   from `api/handler.py`.

**Steps 1-2 — implemented and verified 2026-08-01** (not yet deployed at
verification time; the `line1`/`line2` addition to `GET /satellites` was
pushed ahead of the frontend rewrite so parity could be checked against
real live data). As-built notes:

- **satellite.js v7.1.0**, loaded via jsDelivr's `+esm` CDN endpoint
  (the package is ESM-only as of v7 — no UMD/global bundle exists
  anymore). This required converting `app.js`'s own script tag to
  `type="module"` — verified safe since `config.js` (classic script,
  sets `window.SATTRACK_CONFIG`) always runs before a deferred module
  script regardless of source order.
- `viewer.clock.shouldAnimate = true` is required for the per-frame
  `onTick` render loop to actually advance — the animation/timeline
  widgets are disabled in this app's Viewer config, and without this
  flag the clock silently never ticks forward, so satellites render
  once and then never move. First thing to check if a future edit
  breaks motion again.
- **Parity verified** via a temporary console-comparison against the
  live `/positions` route (satellite.js vs. server Skyfield, same
  instant, all 23 tracked satellites): position deltas landed within
  ~0.02° lat/lon (roughly 1-3 km) and ~50 m altitude — consistent with
  DESIGN.md's own documented "two SGP4 engines can differ slightly"
  caveat, not a bug. Motion was independently confirmed by reading the
  same satellite's computed position twice ~105 s apart and observing a
  real, physically-consistent shift.
- Local pass finder (`findPassesLocal`) verified against a manually
  entered observer: produced a chronologically ordered list of passes
  with plausible period spacing (~97 min apart, matching the selected
  satellite's actual orbital period) and sane elevation/azimuth ranges.
  `localStorage` persistence of the manual/Geolocation observer
  confirmed across a page reload.
- No JS console errors during any of the above.

**Step 3 — implemented and verified 2026-08-01, live.** Widened the
tracked catalog to include CelesTrak's `starlink` group — **10,769**
satellites as of the live fetch that landed this (constellation size
moves; treat that as a snapshot, not a constant). Backend deployed via
PR + `plan.yml` rather than a direct push to `main`, since `tofu` was
locally blocked by an Application Control/endpoint-security policy for
this session — CI's real plan output stood in for the local check.
As-built notes:

- `GET /positions` retired (see the roadmap-interaction note above) —
  confirmed returning 404 live. `GET /satellites` measured at **2.97 MB**
  for all 10,792 satellites (23 stations + 10,769 starlink), comfortably
  under Lambda's 6 MB response limit but a shrinking margin as Starlink
  grows — worth revisiting if/when more groups are added (item 13).
- `tle_fetcher`'s real run (both groups, one invocation) measured
  **20.3 s** billed duration against the new 120 s timeout budget, using
  113 MB of the new 256 MB allocation — real headroom on both axes, the
  30-45 s estimate that sized the timeout was conservative.
- Each catalog item is tagged with its source `group`
  (`stations`/`starlink`) at ingest time, exposed via `GET /satellites` —
  the frontend splits rendering treatment by this field rather than
  guessing from the name.
- Bulk rendering: a separate `Cesium.PointPrimitiveCollection` (not the
  Entity API) for the ~10,769 Starlink points, no labels, not
  individually selectable — by design, confirmed with you before
  building. Propagation is amortized across an `onTick`-driven rolling
  slice (~180 satellites/tick) rather than a once-a-second full batch,
  to avoid a periodic stutter. The existing ~23-satellite stations group
  (Entity API, full interactivity — click, panel, pass prediction) is
  untouched code, confirmed zero regression.
- Confirmed clean: zero console warnings/errors across all 10,792 real
  TLEs — `twoline2satrec`'s try/catch never triggered despite Starlink's
  continuous launch/deorbit churn, at least on this fetch.

**Gotcha (cost a real amount of debugging time): Chrome throttles
`requestAnimationFrame` almost to a halt for backgrounded/hidden tabs —
this looks identical to a severe rendering-performance bug and isn't
one.** Verifying this step's frame rate via the `claude-in-chrome`
automation tooling produced alarming readings (sub-1 FPS, multi-second
frame times) that led down a wrong path — briefly "fixing" things that
were never actually broken (removing point transparency, capping the
bulk count) before the real cause surfaced: `document.hidden` was `true`
and `document.visibilityState` was `"hidden"` for the automated tab, and
even a `requestAnimationFrame`-based frame counter never received a
single callback within a 45-second window, while a plain `fetch()` on
the same tab returned instantly. Cesium's own render loop (and
`clock.onTick`) rides the same throttled callback, so *any* Cesium app
would show this exact symptom in a backgrounded automated tab, entirely
independent of point count, transparency, or update frequency — none of
which were ever the problem. Confirmed the original design (translucent
color, full 10,769 points, amortized updates) was correct all along by
reverting the diagnostic changes and re-verifying: renders correctly,
zero errors, motion and interactivity all check out. **What this means
for future verification**: real sustained frame-rate/smoothness claims
can't be trusted from this automation path — screenshots and short
console checks are fine (they don't depend on a live render loop), but
don't chase FPS numbers from a tab you can't confirm is foregrounded.
Recommend a quick manual look in an actual foreground browser as the
real check for anything performance-sensitive going forward.

**Portfolio framing:** "Moved orbit propagation client-side (satellite.js)
to support arbitrary observer locations and scale to ~10k objects on
CloudFront-cached, observer-independent data instead of added backend
compute, while retaining server-side Skyfield propagation for scheduled
SNS alerts." Server-side vs. client-side compute, with an explicit
rationale and an explicit carve-out.

**Correction to Step 3's "zero console warnings/errors" claim above:**
that check was real but incomplete — it covered TLE *parsing*
(`twoline2satrec`'s try/catch) against one live fetch, not SGP4
*propagation* at every instant against the full swarm over time. A crash
surfaced during Step 4 verification (below) that Step 3's testing never
hit. Documented in full there rather than rewritten here, since it's a
Step 4-discovered bug, not a Step 3 regression.

**Step 4 — implemented and verified 2026-08-04, live.** `GET /satellites`
is observer-independent and only changes as often as Phase 1's 2h fetch
schedule, so every client poll re-scanning DynamoDB for the
Starlink-scale catalog was pure waste — this step makes caching, not
client-side compute, pay for that scale (per the caveat earlier in this
section). As-built notes:

- Added the HTTP API as a second CloudFront origin (`apigw-satellites`)
  on the *existing* frontend distribution from Phase 3, rather than a
  separate distribution — one fewer resource, and it makes the frontend's
  API call same-origin (no CORS preflight) as a free side effect. A
  dedicated `aws_cloudfront_cache_policy` (1h default / 2h max TTL,
  bracketing the fetch cadence) is scoped to just the `/satellites` path
  pattern — deliberately not a wildcard, since the two per-item routes
  had no frontend callers left to cache for anyway (see Step 5).
- The frontend's generated `apiBaseUrl` now points at the CloudFront
  domain instead of the raw execute-api endpoint; the raw endpoint
  (`api_endpoint` output) still works directly for ad-hoc testing.
- Verified live: first request to `/satellites` through CloudFront came
  back `X-Cache: Miss from cloudfront`; a repeat request came back `Hit
  from cloudfront` with `Age` incrementing — confirmed the Lambda isn't
  invoked on cache hits, not just that the config looks right.
- Deployed via PR + `plan.yml`/`apply.yml`, not a local `tofu apply` —
  local plans this session showed the Lambda layer and `tle_fetcher` zip
  as changed/replaced purely from Windows-vs-Linux build drift (the same
  gotcha Phase 5 documented); CI's Linux-built plan came back with
  exactly the 4 intended resource changes and none of that noise,
  confirming it really was local-only.
- **Bug found here, not introduced here:** the Starlink swarm render
  could crash with `TypeError: Cannot read properties of null (reading
  'position')`, reproduced live against the real ~10,769-object swarm.
  Root cause: `satellite.propagate()` can return `null` outright for some
  satrecs — not just `{ position: false }`, which was the only failure
  mode Step 3's `if (!posVel.position) continue;` guard checked — and,
  separately, propagation can "succeed" with NaN-filled ECI components
  that pass a plain truthiness check but produce a NaN `Cartesian3` that
  breaks Cesium's internal bounding-volume math the same way. Fixed with
  a shared `hasValidPosition()` guard (null-check on `posVel` itself,
  plus `Number.isFinite` on each component) used by both render loops and
  the local pass-prediction path, plus a `satrec.error` check at
  TLE-parse time so a dead-on-arrival element set never enters the render
  set at all. Skip logging is throttled — once per satellite per catalog
  refresh for the named 23, once per full ~1s cycle for the bulk swarm —
  since the first version of this fix still logged a persistently-bad
  object on every tick (60x/sec) before that was caught and fixed too.
  Re-verified live post-fix: no crash, steady ~2/10,769 (0.02%) skip
  rate — normal SGP4 edge-case noise per this step's own guardrail, not
  evidence of bad TLE ingestion.

**Step 5 — implemented and verified 2026-08-04, live.** Retired the two
routes Steps 1-2 already made redundant and dropped their only
dependency. As-built notes:

- `GET /satellites/{id}/position` and `GET /satellites/{id}/passes`
  removed from `local.api_routes` (locals.tf) and from
  `src/api/handler.py`'s `ROUTES` map — both now 404, mirroring how
  `GET /positions` already behaved since Step 3.
- `src/api/handler.py` no longer imports Skyfield, numpy, or
  `shared/passes.py` at all — `sattrack-api` is now a pure
  DynamoDB-Scan-and-serialize Lambda. The Skyfield Lambda layer resource
  stays defined in `lambda_api.tf` (moving it to `alerts.tf` felt like
  churn for a personal project, matching this file's existing "don't
  rename/reorganize working things" convention) but is no longer attached
  to `aws_lambda_function.api` — `aws_lambda_function.alerts` is its only
  remaining consumer.
- `sattrack-api`'s IAM policy narrowed from `["dynamodb:GetItem",
  "dynamodb:Scan"]` to just `"dynamodb:Scan"` — `GetItem` only ever
  backed the two now-removed per-item routes.
- `memory_size` deliberately left at 512 MB rather than re-tuned down:
  the original justification (numpy import headroom) is gone, but
  Scan+JSON-serialize of the ~10,800-item catalog is the thing sizing it
  now, and that was proven to fit the 30s timeout (bumped for exactly
  this reason in Step 3) at 512 MB — lowering it untested risked
  reintroducing the timeout risk Step 3 deliberately budgeted against.
- Tests: the two retired routes' request-shape tests (missing lat/lon,
  out-of-range observer, unknown-satellite-404) replaced with two
  retired-route 404 assertions, mirroring the existing `GET /positions`
  one. The shared-module tests (`subpoint_of`, `compute_passes`,
  `azimuth_to_compass`, the numpy-JSON-serialization regression test)
  moved out of `test_api.py` into a new `tests/test_passes.py` — they
  test `shared/passes.py` directly via its own public functions, not any
  API route, and had nothing left to do with the API handler once its
  Skyfield dependency was gone. All 28 tests pass post-split.
- This closes out Phase 6's sequencing list in full (items 1-5 above).

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
   Purely derived from TLEs already in DynamoDB. **If Phase 6 lands
   first:** this becomes a satellite.js concern in the browser, not an
   API response shape — see Phase 6's roadmap-interaction note above.
4. **Extend the pass search window from 48h to 10–14 days.** ISS-class
   visible passes arrive in clusters separated by weeks, so a short window
   legitimately returns zero most of the time. Propagating one satellite
   over two weeks is milliseconds — cost is negligible.

   **Done — implemented and verified 2026-08-04, live.** Landed as a
   `findPassesLocal()` change in `app.js` (Phase 6 already having made
   this a client-side concern, per the roadmap-interaction note above) —
   `PASS_SEARCH_DAYS = 14`. Also updated: the empty-state and hint copy
   (both previously hardcoded "48 h"), and the peak-time display now
   includes month/day, not just weekday — over a 2-week window "Mon"
   alone is ambiguous. Added a "Computing…" busy state (disable the
   button, defer via `requestAnimationFrame` so the label actually
   paints before the synchronous loop runs) anticipating the ~7x jump in
   `satellite.propagate()` calls per click (5,760 → 40,320 at the
   existing 30s step size) — turned out to be unnecessary caution:
   verified live, the full computation for one satellite completes
   fast enough to be visually instant, no perceptible freeze. Kept the
   busy state anyway as cheap insurance against slower devices or any
   future further widening.

   **Side finding, not caused by this item — flagged, not yet fixed:**
   verifying this against a local dev server (a second origin from the
   deployed CloudFront domain) surfaced that Step 4's `/satellites`
   cache behavior doesn't forward the `Origin` header to API Gateway, so
   CORS response headers never come back on cross-origin requests to
   that path — confirmed via `curl -H "Origin: ..."` returning no
   `Access-Control-Allow-Origin`. Harmless for the deployed site itself
   (frontend and API are same-origin through CloudFront as of Step 4),
   but it means the raw `api_endpoint` output is no longer cleanly
   callable cross-origin by anything else (a separate local dev server,
   a future second client). Fix would be adding `Origin` to the
   `satellites_api` cache policy's `headers_config` — not done here,
   out of scope for a pass-window change.
5. **Observer location as a query parameter** —
   `GET /satellites/{id}/passes?lat=&lon=&elev=`, falling back to the SSM
   observer when omitted. Keeps the existing alerting path and any current
   callers working untouched. **Privacy decision:** round coordinates to 2
   decimal places (~1 km) before they enter the query string — more than
   sufficient for pass prediction, and it materially reduces what lands in
   CloudFront/API Gateway access logs. Document this reasoning inline;
   it's the system's answer to "how did you handle location PII?" **If
   Phase 6 lands first:** this route retires outright — the observer
   never leaves the browser, so there's no query param (or its privacy
   rounding) to design at all.
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
   **If Phase 6 lands first:** this classification math (sun altitude,
   twilight thresholds) needs a JS port to run against satellite.js
   output — same logic, different runtime, not a Python change.

   **Done — implemented and verified 2026-08-04, live.** Landed as the JS
   port this item anticipated, in a new `frontend/sun.js` module (imported
   by `app.js`, same pattern as the satellite.js CDN import): a
   low-precision solar position formula (Astronomical Almanac "low
   accuracy" algorithm, ~0.01° precision — no ephemeris download needed,
   unlike the Skyfield/de421 path this mirrors) plus a cylindrical
   Earth-shadow model for the satellite's own sunlit/eclipsed state.
   `findPassesLocal()`'s pass objects now carry a real `visible`
   (true/false, not the `null` placeholder from before) and a `reason`
   (`"daylight"` or `"eclipsed"`) for non-visible ones. Frontend defaults
   to visible-only with a "show all passes" toggle, per spec — toggling
   re-filters an already-computed list rather than re-running the 14-day
   scan, and the empty state distinguishes "nothing overhead at all" from
   "passes exist, none visible" rather than looking identical.

   **Verified correct, not just running:** independently scanned sun
   altitude across 24h at the observer's coordinates (south Florida) and
   confirmed sunrise/solar-noon/sunset all landed within a minute or two
   of real-world expectations for the date/location — including solar
   noon correctly offset ~24 min past clock noon from Florida's longitude
   within the EDT zone, which a sign or frame error would have gotten
   visibly wrong. Then cross-checked the actual pass classifications
   against that scan: every "daylight" pass fell at a time the
   independent scan had the sun above the -6° threshold, and the 4
   "visible" ISS passes found all clustered in the pre-dawn hours (~5 AM
   local) immediately before the calculated sunrise — the classic
   dawn/dusk-terminator clustering real ISS visibility actually has, not
   an artifact. 2 "eclipsed" results also appeared in that same pre-dawn
   window (observer dark, but ISS itself not yet catching sunlight at
   that specific orbital crossing) — the expected boundary-zone mix, not
   an all-or-nothing split.

   **Deliberate scope trim vs. the original spec above:** shipped a
   single civil-twilight threshold (-6°, matching
   `OBSERVER_DARK_SUN_ALTITUDE_DEG` in `shared/passes.py` exactly —
   duplicated across the two runtimes on purpose, not shared, per Phase
   6's "two engines by design" carve-out) rather than the full
   civil/nautical/astronomical set this item originally proposed — civil
   twilight is what the *server-side* alerts Lambda already uses in
   production, so matching it keeps the two verdicts consistent rather
   than introducing a second, different definition of "visible" for the
   same satellite. `too_low` also not implemented as a category: every
   pass in `findPassesLocal()`'s output already cleared its own
   `minElevationDeg` search threshold by construction, so nothing in this
   function's output is actually "too low" — that reason only makes sense
   against a *second*, stricter threshold (e.g. `alerts.tf`'s 30° alert
   bar), which is a "worth texting about" distinction, not a "worth
   showing in a browsable list" one. Revisit if a future increment wants
   that distinction surfaced in the UI too.

   **Incident, same night: SSE-KMS + CloudFront OAC don't mix on an
   AWS-managed key — shipping `sun.js` broke the live site.** The
   `frontend` S3 bucket's default encryption was `aws:kms` (set during
   Phase 5's Checkov hardening pass), but every object already in the
   bucket still carried the *older* `AES256` encryption from before that
   change — S3's bucket-default encryption only applies going forward,
   and apparently existing keys don't necessarily pick up a new default
   on a content-only overwrite either (confirmed empirically: `app.js`,
   re-uploaded in the very same `apply` as `sun.js`, kept `AES256`;
   `sun.js`, a genuinely new key, got `aws:kms`). CloudFront's Origin
   Access Control cannot decrypt SSE-KMS objects encrypted with an
   AWS-managed key without an extra key-policy grant scoped to the
   distribution — and AWS-managed keys' policies aren't customer-editable
   the way a customer-managed CMK's is, so that grant can't be added here
   without switching to a paid CMK (a cost this project's Checkov posture
   deliberately declined, see `.checkov.yaml`). Net effect: `sun.js`
   403'd from CloudFront, and since `app.js` hard-`import`s it, **the
   entire site failed to load** for a few minutes — this wasn't a
   degraded feature, item 8 shipping broke Phase 6 through Step 5 too.
   Diagnosed by comparing `head-object` output across working vs. broken
   files (same bucket, same policy, only the `ServerSideEncryption` field
   differed) — ruled out cache staleness, KMS key policy gaps (the
   AWS-managed key's policy already grants decrypt to any authorized
   in-account S3 caller; that's not what OAC needs), and the CloudFront
   distribution being mid-deploy (it wasn't) before landing on the real
   cause. Fixed in two parts: (1) `aws s3 cp` self-copy to immediately
   re-encrypt the live `sun.js` object as `AES256`, unblocking the site
   in minutes without waiting on a PR; (2) changed
   `aws_s3_bucket_server_side_encryption_configuration.frontend`'s
   `sse_algorithm` to `AES256` to prevent recurrence for any future new
   file — `tle_archive`'s bucket (main.tf) is unaffected and correctly
   stays on `aws:kms`, since it's never served through CloudFront/OAC.
   A second, unrelated staleness bug surfaced during the same
   verification pass: CloudFront caches `index.html` and `app.js`
   independently (each with its own TTL), so an edge node can serve a
   *stale* `index.html` alongside a *fresh* `app.js` — this manifested as
   `document.getElementById("passes-show-all")` returning `null` (the
   fresh `app.js` expected markup the stale `index.html` didn't have
   yet), crashing the toggle wire-up. Fixed with a full `/*` CloudFront
   invalidation rather than chasing individual paths. **Lesson for future
   frontend deploys:** a brand-new file being added to the S3 bucket, or
   any deploy under real time pressure, is worth a live-browser check
   with a hard reload immediately after `apply.yml` completes — this
   project's existing CDN caching (Phase 3's 5-min TTL, Phase 6 Step 4's
   1-2h TTL) does not guarantee two files in the same deploy propagate to
   the same edge node atomically together.
9. **"Next visible pass" hero field** at the top of the panel, once #8
   lands — this is the question users are actually asking. Handle the
   honest empty case explicitly: no visible passes in the next N days,
   with a one-line explanation that visibility comes in clusters. **If
   Phase 6 lands first:** "once #8 lands" means the JS port above, not
   the API version.

   **Done — implemented and verified 2026-08-06, live.** A new
   `#passes-hero` element sits between the "Upcoming passes" heading and
   the existing hint text in `index.html`; `app.js`'s `renderHero()`
   populates it from the same `passes` array `renderPasses()` already
   computes — no separate query or propagation pass. Deliberately reads
   from the *unfiltered* list (`passes.find(p => p.visible)`), not the
   "show all passes" toggle's filtered view: the hero always answers the
   visible-pass question even while the list below is showing everything,
   since those are two different questions ("what's the next one I can
   see" vs. "show me all the geometry"). Empty case matches the backlog
   item's exact ask — "No visible passes in the next 14 days — visibility
   comes in clusters, so this is normal" — styled muted/italic
   (`.hero-empty`) vs. the found case's highlighted accent box
   (`.hero-active`), so the honest-empty state doesn't read as an error.
   Hero clears (`hidden = true`) alongside the existing
   `passesList`/`lastComputedPasses` reset in the `selectedEntityChanged`
   handler, so it can't show a stale answer for a satellite that's no
   longer selected.

   Verified against the real UI, not just read through: no existing dev
   workflow serves the frontend with live data locally (Item 4's flagged
   CORS gap blocks a separate-origin dev server from calling
   `/satellites` at all), so a throwaway same-origin mock server
   (`http.server` subclass serving `frontend/` plus a hand-rolled
   `/satellites` response with one real ISS TLE and a `config.js` built
   from `window.location.origin`) stood in for the deployed CloudFront
   setup. Selected the ISS via the search box, entered manual observer
   coordinates, clicked "Predict passes here," and confirmed in-browser:
   the hero matches pass #1 in the list below, stays fixed when "show all
   passes" is toggled, and disappears when the entity is deselected.
   Scratch server and mock TLE were local-only, not committed.
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

    **Done — implemented and verified 2026-08-06, live.** Bound to
    shift-click, not a plain click — Cesium's `Viewer` already registers
    its own unmodified `LEFT_CLICK` handler internally (that's the
    existing satellite-selection behavior), and `setInputAction` on the
    same `(type, modifier)` pair replaces whatever was already bound
    there with no public API to recover the original. A distinct
    `KeyboardEventModifier.SHIFT` registers as a genuinely separate
    handler instead, so the two can never collide — confirmed live by
    clicking the ISS entity (still selects normally) immediately before
    and after adding the pin handler, not just by reasoning about the
    API. `camera.pickEllipsoid()` turns the click into a
    `Cartographic`, feeds the existing `setActiveObserver()` (so it gets
    `localStorage` persistence and the "Observer: ..." status line for
    free) with a `"map pin"` source label, and also fills the manual
    lat/lon inputs so the dropped point is visible and hand-editable
    afterward. Off-globe clicks (into space, past the limb) are a no-op
    — `pickEllipsoid()` returns `undefined` there, guarded before
    touching `Cartographic.fromCartesian`. A one-line hint
    (`#obs-pin-hint`, "shift-click the globe to drop a pin there") sits
    above the existing observer controls, since the interaction has no
    other affordance a user would discover on their own.

    Verified against the same throwaway local mock server as item 9 (real
    ISS TLE, same-origin `/satellites` stub — no dev workflow exists yet
    for live data against a non-CloudFront origin, per item 4's flagged
    CORS gap): shift-clicking open ocean set the observer to the clicked
    lat/lon and updated the manual inputs, a plain click on the ISS point
    immediately before and after still selected the entity correctly, and
    console stayed clean. Re-verified the same two checks live on the
    deployed CloudFront site after `apply.yml` completed — a shift-click
    set the observer even before any satellite was selected (persisted
    and displayed correctly once a panel was opened), and a plain click
    on a rendered point still selected its entity (`PROGRESS-MS 34`).
12. **Inclination-limit display.** An observer poleward of a satellite's
    inclination never gets a high pass (ISS at 51.6°, Tiangong at ~41.5°).
    No filtering change needed — showing compass bearing and peak
    elevation (already true once #3 lands) makes a permanently-low pass
    legible instead of looking like a bug.

    **Done — implemented and verified 2026-08-07, live.** Item 3's own
    text (`compass` + peak elevation already in every pass record) turned
    out to only be half the fix — a user staring at a run of low-elevation
    passes, or the "no visible passes" empty state, still has to *notice*
    the pattern and connect it to orbital mechanics themselves. Built the
    explicit version instead: `maxSubpointLatitudeDeg()` derives the
    ground track's true latitude extremity from `satrec.inclo` —
    inclination itself for a prograde orbit (<=90°), `180 - inclination`
    for a retrograde/sun-synchronous one (a ~98° orbit tops out at 82°
    latitude, not 98 — got this wrong on the first pass reasoning through
    it, worth double-checking against a real sun-synchronous TLE if this
    code changes again). When `abs(observerLat) > maxSubpointLatitudeDeg`,
    a new `#passes-inclination-note` renders an explicit sentence (e.g.
    "This satellite's orbit only reaches 51.6° latitude — from 68.0°N,
    expect passes to stay low on the horizon, if it rises at all.");
    otherwise it stays hidden, same lifecycle as item 9's hero (computed
    once per `computeAndRenderPasses()` call, cleared on satellite
    change). Deliberately a separate element from the hero rather than
    folded into it — the hero answers "when," this answers "is this
    combination even geometrically sane," and they can both be true or
    false independently. No filtering change, matching the backlog item's
    own framing — every pass is still listed either way.

    Verified against the same local mock server as items 9/11 (real ISS
    TLE, inclination 51.6416° per its TLE line 2): observer at 68°N, well
    poleward of that line, correctly triggered the note and matched
    reality — the list genuinely showed zero passes above 10° in the
    14-day window. Observer at 38.9°N (well equatorward of the line)
    correctly hid the note and showed real passes up to 77° peak
    elevation, confirming no false positive for the common case. Console
    stayed clean throughout. Re-verified live on the deployed CloudFront
    site after `apply.yml` completed: observer at 75°N against the real
    ISS TLE showed the note and a genuine zero-passes result, matching
    the local check.
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

    **Done — implemented and verified 2026-08-04, live.** `CELESTRAK_GROUP`
    widened to `"visual,stations,starlink"` (`main.tf`) — CelesTrak's
    `visual` group measured **157** objects live at deploy time (optically
    brightest, including bright rocket bodies/debris, not just active
    satellites — "~100" was always an approximation). No frontend code
    change needed: `app.js`'s existing `sat.group === "starlink" ? bulk :
    interactive` split already routes anything that isn't `starlink` to
    the full Entity-based interactive path, so `visual` satellites get
    click/panel/pass-prediction for free. Deliberately did not touch
    `alert_watchlist` (still ISS-only) — this item widens what the globe
    tracks and what pass prediction can be run against, not what triggers
    an SMS/email; that's a separate, more consequential decision. Order
    matters in the env var: `visual` overlaps `stations` on 2 objects
    (ISS, Tiangong — both groups list them), and `write_satellites`'s
    per-group overwrite means whichever group is processed last for a
    given NORAD ID wins the `group` tag; `visual` is listed first so
    `stations` (the more specific, correct tag) wins that overwrite. No
    functional effect today either way — both tags route through the
    same non-`starlink` render path — but it keeps the tag itself
    accurate for whenever something *does* read it (item 14's
    docked-object handling, potentially). Stopped at `visual` per this
    item's own cost note — `gnss`/`geo`/`starlink`-adjacent groups not
    added.
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
15. **`GET /overhead?lat&lon` route** — "what's above me right now." **If
    Phase 6 lands first:** likely answered client-side by filtering the
    already-fetched TLE catalog against the browser's own location
    instead of a new server route — re-evaluate whether this needs a
    backend endpoint at all once satellite.js is in place.

    **Done — implemented and verified 2026-08-14, live.** Landed exactly
    as anticipated above: no new route, `refreshOverhead()` in `app.js`
    re-runs the existing `lookAngles()`/`classifyVisibility()` pair
    (already written for the passes panel) against the current clock
    time instead of a 14-day scan, on the same 1s panel-refresh throttle
    as `refreshPanel()`. Scoped to the named `catalog` only (stations +
    visual, ~180 objects) — the bulk Starlink swarm has no name loaded
    client-side and would swamp a "what's up" list with unlabeled
    points. The observer-location form moved out of the satellite
    click-panel into its own always-visible `#observer` aside on the
    left, giving the overhead list a permanent home independent of any
    selection.

    **Side finding, not caused by this item — found and fixed the same
    session:** first live-verification pass showed no visible change at
    all. Root cause wasn't the deploy — `curl` confirmed CloudFront was
    serving the new bytes immediately — it was the browser's own HTTP
    cache: `aws_s3_object.frontend` never set a `Cache-Control` header,
    so with none present browsers fell back to RFC 7234 heuristic
    freshness instead of the 5-minute worst-case staleness
    `aws_cloudfront_cache_policy.frontend`'s own comment already assumed.
    A returning browser kept rendering a week-old `app.css` with no
    `#observer` positioning rule, so the new panel rendered as an
    unstyled full-width block pushed below the fold. Fixed by adding
    `cache_control = "public, max-age=300, must-revalidate"` to both
    `aws_s3_object.frontend` and `frontend_config`, matching the CDN
    edge's existing `default_ttl`, in a separate PR — this bug would
    otherwise have recurred on every future frontend deploy for any
    returning visitor.

    That fix's own `apply.yml` run hit a second, apparently transient
    issue: 4 of 5 objects updated cleanly, then `sun.js` failed with
    `InvalidArgument: Server Side Encryption with AWS KMS managed key
    requires HTTP header x-amz-server-side-encryption: aws:kms` — despite
    the bucket's default encryption confirmed as AES256 throughout (`aws
    s3api get-bucket-encryption`) and no KMS-enforcing bucket policy
    anywhere (`aws s3api get-bucket-policy` showed only the CloudFront
    OAC `GetObject` allow). An identical manual `PutObject` succeeded
    immediately after with no code or config change, so this reads as a
    one-off S3-side blip, not a real constraint — flagged here in case it
    recurs, not chased further. Re-running the failed `apply.yml` job
    came back `0 added, 0 changed, 0 destroyed`, confirming state,
    live content, and the manual interim fix all agreed.

    Verified correct (not just deployed) after the cache fix: hard
    reload showed the "Observer location" panel correctly positioned and
    styled on the left with a live "Overhead now" list (elevation,
    compass bearing, visibility verdict, updating on the panel's normal
    tick); clicking a satellite still opened the right-side selection
    panel with no layout collision between the two; console stayed
    clean.

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
