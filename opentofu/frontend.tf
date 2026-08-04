# -----------------------------------------------
# Phase 3 — CesiumJS globe on S3 + CloudFront
#
# Private bucket, CloudFront with Origin Access Control — the canonical
# static-site pattern: nothing in S3 is world-readable; only this
# distribution can fetch objects.
# -----------------------------------------------

locals {
  frontend_dir = "${path.module}/../frontend"

  frontend_mime_types = {
    ".html" = "text/html"
    ".css"  = "text/css"
    ".js"   = "text/javascript"
    ".png"  = "image/png"
    ".ico"  = "image/x-icon"
  }
}

resource "aws_s3_bucket" "frontend" {
  # checkov:skip=CKV2_AWS_61: static site bucket — OpenTofu overwrites the
  # same keys in place on every deploy, nothing accumulates that needs expiry.
  # checkov:skip=CKV_AWS_145: AES256 (SSE-S3), not KMS, is deliberate here —
  # see the aws_s3_bucket_server_side_encryption_configuration.frontend
  # resource below for why (CloudFront OAC can't decrypt SSE-KMS on an
  # AWS-managed key, and this project's cost posture already declined
  # customer-managed CMKs elsewhere — same tradeoff, applied consistently).
  bucket = "${local.name_prefix}-frontend-${data.aws_caller_identity.current.account_id}"

  tags = {
    Name = "${local.name_prefix}-frontend"
  }
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# AES256 (SSE-S3), not aws:kms — deliberately different from the other
# buckets in this project (tle_archive uses the AWS-managed KMS key with
# zero issues). This bucket is served through CloudFront's Origin Access
# Control, and OAC cannot decrypt SSE-KMS objects encrypted with an
# AWS-managed key: AWS requires an extra key-policy grant scoped to the
# distribution for that to work (see AWS's own CloudFront + OAC + S3 docs
# on the SSE-KMS caveat), and AWS-managed keys' policies aren't
# customer-editable the way a customer-managed CMK's is — this project's
# own cost-driven "AWS-managed key, not a customer CMK" choice (see
# .checkov.yaml) is exactly what makes that extra grant impossible here.
# Discovered the hard way 2026-08-04: every object already in this bucket
# still carried AES256 from before this resource briefly set aws:kms
# during the Phase 5 Checkov pass, silently masking the incompatibility —
# it only surfaced when sun.js became the first genuinely new key
# written since, and CloudFront 403'd it. AES256 still satisfies Checkov's
# general "encrypted at rest" check; it's specifically a *customer-managed
# KMS key* skip (already accepted in .checkov.yaml for other resources)
# this project isn't paying for, not an unencrypted bucket.
resource "aws_s3_bucket_server_side_encryption_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Repo files, uploaded as-is. config.js is deliberately absent here — it is
# generated below from deploy-time values, never committed.
resource "aws_s3_object" "frontend" {
  for_each = fileset(local.frontend_dir, "**")

  bucket = aws_s3_bucket.frontend.id
  key    = each.value
  source = "${local.frontend_dir}/${each.value}"
  etag   = filemd5("${local.frontend_dir}/${each.value}")
  content_type = lookup(
    local.frontend_mime_types,
    ".${reverse(split(".", each.value))[0]}",
    "application/octet-stream"
  )
}

resource "aws_s3_object" "frontend_config" {
  bucket       = aws_s3_bucket.frontend.id
  key          = "config.js"
  content_type = "text/javascript"
  # Phase 6 Step 4: routed through this same CloudFront distribution
  # (see the "apigw-satellites" origin/behavior below) instead of the raw
  # execute-api endpoint, so GET /satellites is CDN-cached and the browser
  # call becomes same-origin (no CORS preflight). The raw endpoint (see the
  # api_endpoint output) still works directly for ad-hoc testing.
  content = <<-EOT
    window.SATTRACK_CONFIG = {
      apiBaseUrl: "https://${aws_cloudfront_distribution.frontend.domain_name}",
      cesiumIonToken: "${var.cesium_ion_token}",
    };
  EOT
}

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${local.name_prefix}-frontend"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Short TTLs instead of the managed CachingOptimized policy: this site gets
# redeployed while phases are being built, and a 5-minute worst-case
# staleness beats issuing an invalidation on every apply.
resource "aws_cloudfront_cache_policy" "frontend" {
  name        = "${local.name_prefix}-frontend"
  default_ttl = 300
  max_ttl     = 3600
  min_ttl     = 0

  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true
  }
}

# Phase 6 Step 4: GET /satellites is observer-independent (same payload for
# every viewer) and only changes as often as Phase 1's fetch schedule —
# caching it turns Starlink-scale catalog scans from a per-poll DynamoDB
# cost into a per-cache-miss one. TTL brackets the 2h fetch cadence
# (main.tf's aws_scheduler_schedule.tle_fetcher): no point serving data
# fresher than the source refreshes, but also no point holding it a full
# cycle past a refresh.
resource "aws_cloudfront_cache_policy" "satellites_api" {
  name        = "${local.name_prefix}-satellites-api"
  default_ttl = 3600
  max_ttl     = 7200
  min_ttl     = 0

  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true
  }
}

resource "aws_cloudfront_distribution" "frontend" {
  # checkov:skip=CKV2_AWS_32: response_headers_policy_id below is set to
  #   the real Managed-SecurityHeadersPolicy ID (see locals.tf) — this
  #   check just can't see it because it only recognizes a graph
  #   connection to a response_headers_policy resource/data source, not a
  #   literal ID. A data-source lookup was tried first and reverted: it
  #   breaks CI's own bootstrapping, since gha_apply's data-source reads
  #   happen before that same apply's IAM policy grant takes effect.
  # checkov:skip=CKV_AWS_86: access logging needs a dedicated log bucket
  #   (plus its own lifecycle/encryption) for a personal static site — not
  #   worth the added bucket and cost.
  # checkov:skip=CKV_AWS_310: origin failover needs a second S3 origin; one
  #   bucket is the whole site, nothing to fail over to.
  # checkov:skip=CKV_AWS_374: public tracker site, no geographic restriction
  #   is called for.
  # checkov:skip=CKV_AWS_174: minimum_protocol_version isn't configurable
  #   with cloudfront_default_certificate — needs a custom domain + ACM
  #   cert, which this project doesn't have (serves off *.cloudfront.net).
  # checkov:skip=CKV_AWS_68: CloudFront WAF bills per web ACL/rule/request;
  #   API Gateway throttling (api_gateway.tf) already caps the blast radius
  #   for a hobby project with no attack surface beyond static assets.
  # checkov:skip=CKV2_AWS_42: custom SSL cert needs the same custom domain
  #   as CKV_AWS_174 above — not in place.
  # checkov:skip=CKV2_AWS_47: depends on the WAF this project deliberately
  #   skips (CKV_AWS_68).
  enabled             = true
  comment             = "${local.name_prefix} globe"
  default_root_object = "index.html"
  price_class         = "PriceClass_100" # NA + EU is plenty for a backyard tracker

  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "s3-frontend"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  # execute-api domains are HTTPS-only, hence the custom_origin_config
  # (not another origin_access_control, which is S3-specific).
  origin {
    domain_name = replace(aws_apigatewayv2_api.sattrack.api_endpoint, "https://", "")
    origin_id   = "apigw-satellites"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id           = "s3-frontend"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.frontend.id
    response_headers_policy_id = local.cloudfront_managed_security_headers_policy_id
    compress                   = true
  }

  # No wildcard: HTTP API's only frontend-facing route today is exactly
  # GET /satellites (Phase 6 Step 3 retired /positions; the two remaining
  # per-item routes are unused by the browser and deliberately left off
  # this cached path — see Step 5 in DESIGN.md for retiring them outright).
  ordered_cache_behavior {
    path_pattern               = "/satellites"
    target_origin_id           = "apigw-satellites"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.satellites_api.id
    response_headers_policy_id = local.cloudfront_managed_security_headers_policy_id
    compress                   = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = {
    Name = "${local.name_prefix}-frontend"
  }
}

# Only CloudFront (this exact distribution) may read the bucket.
resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.frontend.arn}/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = aws_cloudfront_distribution.frontend.arn
        }
      }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.frontend]
}
