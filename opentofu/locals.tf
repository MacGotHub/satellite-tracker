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
  # A map (not separate resource blocks) so adding a route in Phase 3/4 is
  # a one-line change here.
  api_routes = toset([
    "GET /satellites",
    "GET /satellites/{id}/position",
    "GET /positions",
    "GET /satellites/{id}/passes",
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
}
