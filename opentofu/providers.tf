terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
    # tls dropped with oidc-cicd v0.3.0 — the module no longer does a live
    # data.tls_certificate read for the OIDC provider thumbprint, and
    # nothing else here uses it. (A stale tls entry may linger in
    # .terraform.lock.hcl until the next `tofu init -upgrade`; harmless.)
    time = {
      source  = "hashicorp/time"
      version = "~> 0.13"
    }
  }
}

provider "aws" {
  region = "us-east-1"

  # Applied to every resource (Phase 1's included — expect one-time
  # in-place tag updates on them the first plan after this lands).
  default_tags {
    tags = local.common_tags
  }
}
