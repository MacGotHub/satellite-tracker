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
