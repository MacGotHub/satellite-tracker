# satellite-tracker

A real-time satellite tracking website: live satellite positions animated on a
CesiumJS 3D globe, backed by a serverless AWS pipeline (Lambda + DynamoDB +
API Gateway) that ingests orbital data from CelesTrak, and an alerting Lambda
that sends an SMS when a visible ISS pass is coming up over the house. Built
entirely on AWS with OpenTofu and deployed via GitHub Actions with OIDC —
partly because it's a genuinely useful thing to go outside and watch with Cam,
and partly as a DevSecOps portfolio piece that demonstrates automated, secure,
professional-grade cloud delivery.

## Where to look

| File | What it's for |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Persistent project context — owner, tooling, conventions, current status. Read this first before making any changes. |
| [`DESIGN.md`](DESIGN.md) | Architecture and design rationale — topology, phase plan, build order, open decisions. |

## Status

Phases 1–3 are deployed and running. Phase 1: a Lambda fetches TLEs from
CelesTrak every 2 hours, archives the raw response to S3, and upserts parsed
satellites into DynamoDB. Phase 2: an HTTP API computes live positions and
visible-pass predictions from those TLEs with Skyfield (as a Lambda layer).
Phase 3: a CesiumJS globe on S3 + CloudFront animating live positions, with
click-through pass predictions. Phases 4–5 (SMS alerts, CI/CD) are not
started. See CLAUDE.md for detailed status.

> Phase 1 was originally built on 2026-07-10 as `lab8-sattrack` inside the
> sibling `aws-iac-lab` repo (a planning-doc fork caused by a PC crash) and
> consolidated into this standalone project on 2026-07-16. The deployed AWS
> resources and remote state were untouched by the move — `tofu plan` was
> verified clean from this location.
