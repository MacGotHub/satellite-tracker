# satellite-tracker

A real-time satellite tracking website: ~10,900 tracked objects (ISS and crew
vehicles, ~157 other optically-bright satellites, and the full Starlink
constellation) animated live on a CesiumJS 3D globe, propagated client-side
from CelesTrak TLEs — no server round-trip per view. A serverless AWS
pipeline (Lambda + DynamoDB + API Gateway + CloudFront) keeps that catalog
fresh every 2 hours and enriches it with real CelesTrak SATCAT metadata
(object type, owner, launch date), and a separate alerting Lambda emails
(SMS pending carrier registration) when a visible ISS pass is coming up over
the house. Built entirely on AWS with OpenTofu and deployed via GitHub
Actions with OIDC — partly because it's a genuinely useful thing to go
outside and watch with Cam, and partly as a DevSecOps portfolio piece that
demonstrates automated, secure, professional-grade cloud delivery.

## Where to look

| File | What it's for |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Persistent project context — owner, tooling, conventions, current status. Read this first before making any changes. |
| [`DESIGN.md`](DESIGN.md) | Architecture and design rationale — topology, phase plan, build order, open decisions. |

## Status

All 6 phases are deployed and live, plus an ongoing backlog of enhancements
past that. Phase 1: a Lambda fetches TLEs from CelesTrak every 2 hours,
archives the raw response to S3, and upserts parsed satellites into
DynamoDB — enriched with real CelesTrak SATCAT metadata (object type, owner,
launch date). Phase 2: a read-only HTTP API serves that catalog. Phase 3: a
CesiumJS globe on S3 + CloudFront. Phase 4: a scheduled Lambda emails
(SMS pending carrier registration) when a visible ISS pass is coming.
Phase 5: GitHub Actions + OIDC CI/CD, no static AWS keys. Phase 6 moved
position/pass computation client-side (satellite.js in the browser, not a
server round-trip per view) and widened the tracked catalog to ~10,900
objects. Since then: docked/attached-object flagging, a relative brightness
ranking per pass, live weather radar and cloud-cover-aware visibility
(next 48h), a trailing orbit path for the selected satellite, and a
mobile-responsive layout. See CLAUDE.md for detailed, dated status on every
one of these.

> Phase 1 was originally built on 2026-07-10 as `lab8-sattrack` inside the
> sibling `aws-iac-lab` repo (a planning-doc fork caused by a PC crash) and
> consolidated into this standalone project on 2026-07-16. The deployed AWS
> resources and remote state were untouched by the move — `tofu plan` was
> verified clean from this location.
