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
}
