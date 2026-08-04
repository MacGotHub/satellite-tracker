locals {
  # Naming prefix for every resource. Phase 1 resources were created with
  # literal "sattrack-" names before this file existed — the prefix matches
  # them exactly so nothing gets renamed (a rename is a destroy/recreate).
  name_prefix = "sattrack"

  # Common tags land on every resource via the provider default_tags block
  # in providers.tf, so individual resources only add their Name tag.
  common_tags = {
    Project   = "satellite-tracker"
    ManagedBy = "opentofu"
    Repo      = "MacGotHub/satellite-tracker"
  }

  # HTTP API routes, all handled by the single position-API Lambda.
  # A map (not separate resource blocks) so adding a route back is a
  # one-line change here. GET /positions was retired in Phase 6 Step 3;
  # GET /satellites/{id}/position and GET /satellites/{id}/passes followed
  # in Step 5 — the frontend moved all position/pass math to client-side
  # satellite.js in Steps 1-2, leaving every route but the raw TLE catalog
  # with no consumers.
  api_routes = toset([
    "GET /satellites",
  ])

  # Phase 4 — alerting. The knobs Derek is most likely to tune live here,
  # not in alerts.tf or the handler.
  alert_watchlist              = ["25544"] # NORAD IDs to alert on (ISS)
  alert_min_peak_elevation_deg = 30        # texting bar; the API keeps its 10° horizon
  alert_lead_minutes           = 20        # upper bound — with a 10-min tick, actual lead lands 10–20 min
  alert_tick_minutes           = 10
  digest_lookahead_hours       = 72
  digest_schedule_cron         = "cron(0 17 * * ? *)" # 5 PM America/New_York (set on the schedule)
  observer_ssm_parameter       = "/sattrack/observer" # SecureString "lat,lon", created out-of-band

  # Phase 5 — GitHub OIDC trust policy scoping. GitHub's token `sub` claim
  # for this repo comes back as `repo:MacGotHub@188585672/satellite-tracker@1305326446:...`
  # — the newer immutable-ID format (owner/repo name suffixed with their
  # numeric IDs), confirmed by temporarily logging a decoded token from
  # the plan workflow. Matching on the numeric IDs instead of the plain
  # name is strictly tighter: a rename or a delete-and-recreate under the
  # same name can't produce a token that matches an old policy.
  github_oidc_sub_prefix = "repo:MacGotHub@188585672/satellite-tracker@1305326446"

  # AWS-managed CloudFront response headers policy (HSTS,
  # X-Content-Type-Options, X-Frame-Options, Referrer-Policy) —
  # "Managed-SecurityHeadersPolicy". Hardcoded rather than looked up via a
  # data source: the ID is a global AWS constant, identical in every
  # account/region, since it's an AWS-owned resource, not a customer one.
  # A live lookup also can't work in gha_apply anyway — data sources are
  # read before the same apply's own IAM policy changes take effect, so a
  # first-ever apply granting the read permission would AccessDenied on
  # itself (confirmed the hard way).
  cloudfront_managed_security_headers_policy_id = "67f7725c-6f97-4210-82d7-5512b31e9d03"
}
