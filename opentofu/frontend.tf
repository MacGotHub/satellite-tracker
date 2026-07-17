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
  content      = <<-EOT
    window.SATTRACK_CONFIG = {
      apiBaseUrl: "${aws_apigatewayv2_api.sattrack.api_endpoint}",
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

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  comment             = "${local.name_prefix} globe"
  default_root_object = "index.html"
  price_class         = "PriceClass_100" # NA + EU is plenty for a backyard tracker

  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "s3-frontend"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-frontend"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = aws_cloudfront_cache_policy.frontend.id
    compress               = true
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
