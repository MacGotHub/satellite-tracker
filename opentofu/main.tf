# -----------------------------------------------
# Data sources
# -----------------------------------------------

data "aws_caller_identity" "current" {}

# -----------------------------------------------
# DynamoDB — single table for TLEs (and future entity
# types: pass predictions, alert subscriptions, etc.)
# pk = norad_id, sk = record type ("TLE" for now)
# -----------------------------------------------

resource "aws_dynamodb_table" "sattrack" {
  name         = "sattrack"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  # Phase 4 dedupe flags (sk = ALERT#/DIGEST#<rise time>) carry an epoch
  # expires_at so DynamoDB clears them ~a week after the pass; TLE items
  # never set the attribute and are untouched by TTL.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # Switches from the invisible AWS-owned default key to the AWS-managed
  # alias/aws/dynamodb key — still free, but now a real KMS key with its
  # own CloudTrail usage trail. Checkov's CKV_AWS_119 specifically wants a
  # customer-managed CMK ($1/mo); see .checkov.yaml for why that's skipped.
  server_side_encryption {
    enabled = true
  }

  # Table is a couple dozen items; PITR bills on backup storage, which is
  # negligible at this size, for real protection against a bad apply/bug
  # wiping the catalog.
  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Name = "sattrack"
  }

  # dynamodb:UpdateContinuousBackups (for point_in_time_recovery above) is
  # granted by gha_write_bootstrap (cicd_oidc.tf), not the main gha_write
  # policy — that policy is too cross-referenced to safely depend on
  # without a cycle. Waits on the propagation delay too, not just the
  # attachment: the grant existing isn't enough, IAM needs a moment to
  # actually honor it (see the time_sleep comment in cicd_oidc.tf).
  depends_on = [time_sleep.gha_write_bootstrap_propagation]
}

# -----------------------------------------------
# S3 — raw TLE archive (one object per fetch, for audit/history)
# -----------------------------------------------

resource "aws_s3_bucket" "tle_archive" {
  bucket = "sattrack-tle-archive-${data.aws_caller_identity.current.account_id}"

  tags = {
    Name = "sattrack-tle-archive"
  }
}

resource "aws_s3_bucket_public_access_block" "tle_archive" {
  bucket = aws_s3_bucket.tle_archive.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# AWS-managed key (aws/s3), not a customer CMK — same protection Checkov's
# KMS check wants, zero monthly key cost.
resource "aws_s3_bucket_server_side_encryption_configuration" "tle_archive" {
  bucket = aws_s3_bucket.tle_archive.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

# This is a running archive (one object per fetch, every 2 hours forever)
# with no reader beyond ad-hoc audit — without an expiry it grows without
# bound. 90 days covers any plausible "what did CelesTrak send us" lookback.
resource "aws_s3_bucket_lifecycle_configuration" "tle_archive" {
  bucket = aws_s3_bucket.tle_archive.id

  rule {
    id     = "expire-after-90-days"
    status = "Enabled"

    # Applies to every object in the bucket — required by the provider
    # even when there's nothing to filter on.
    filter {}

    expiration {
      days = 90
    }

    # Belt-and-suspenders: cleans up any multipart upload that never
    # completed (e.g. a killed Lambda mid-PutObject) instead of leaving
    # orphaned parts billed forever.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # s3:PutLifecycleConfiguration comes from gha_write_bootstrap — same
  # reasoning as the DynamoDB table's depends_on above.
  depends_on = [time_sleep.gha_write_bootstrap_propagation]
}

# -----------------------------------------------
# Lambda package — zipped from the handler source, no
# external deps needed (stdlib + boto3, which ships in
# the Lambda Python runtime already)
# -----------------------------------------------

data "archive_file" "tle_fetcher" {
  type        = "zip"
  source_file = "${path.module}/../src/tle_fetch/handler.py"
  output_path = "${path.module}/build/tle_fetcher.zip"
}

# -----------------------------------------------
# IAM — Lambda execution role
# -----------------------------------------------

resource "aws_iam_role" "tle_fetcher" {
  name = "sattrack-tle-fetcher"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "tle_fetcher_logs" {
  role       = aws_iam_role.tle_fetcher.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "tle_fetcher" {
  name = "sattrack-tle-fetcher-access"
  role = aws_iam_role.tle_fetcher.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:BatchWriteItem"]
        Resource = aws_dynamodb_table.sattrack.arn
      },
      {
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.tle_archive.arn}/*"
      }
    ]
  })
}

# -----------------------------------------------
# Lambda — tle_fetcher
# -----------------------------------------------

resource "aws_lambda_function" "tle_fetcher" {
  function_name = "sattrack-tle-fetcher"
  role          = aws_iam_role.tle_fetcher.arn
  handler       = "handler.handler"
  runtime       = "python3.12"
  # Phase 6 Step 3 added "starlink" (~10,800 satellites) alongside
  # "stations". batch_writer() chunks into groups of 25, so that's ~430
  # sequential write calls — plausibly 10-45s alone, on top of a larger
  # HTTP fetch/S3 archive and now two groups instead of one. 120s is real
  # margin (Lambda bills actual duration, not the ceiling); the memory
  # bump gives the batching/JSON work a bit more CPU share.
  timeout          = 120
  memory_size      = 256
  filename         = data.archive_file.tle_fetcher.output_path
  source_code_hash = data.archive_file.tle_fetcher.output_base64sha256

  environment {
    variables = {
      TABLE_NAME  = aws_dynamodb_table.sattrack.name
      BUCKET_NAME = aws_s3_bucket.tle_archive.bucket
      # "visual" (~157 optically-brightest objects incl. rocket bodies,
      # not just active satellites) added — DESIGN.md backlog item 13.
      # Order matters here, not just style: "visual" overlaps "stations"
      # on ISS and Tiangong (both groups list them), and write_satellites
      # overwrites the same pk/sk per group processed — putting "visual"
      # first means "stations" (the more specific, correct tag for an
      # actual space station) wins that overwrite. Stopping at "visual"
      # deliberately, not proceeding to gnss/geo — see item 13's own
      # cost note on Starlink-scale groups being the one thing here that
      # could move the DynamoDB storage/read needle.
      CELESTRAK_GROUP = "visual,stations,starlink"
    }
  }

  tags = {
    Name = "sattrack-tle-fetcher"
  }
}

resource "aws_cloudwatch_log_group" "tle_fetcher" {
  name              = "/aws/lambda/${aws_lambda_function.tle_fetcher.function_name}"
  retention_in_days = 14
}

# -----------------------------------------------
# EventBridge Scheduler — fetch every 2 hours
# (CelesTrak asks that consumers not poll more often than that)
# -----------------------------------------------

resource "aws_iam_role" "scheduler_invoke" {
  name = "sattrack-scheduler-invoke"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name = "sattrack-scheduler-invoke-lambda"
  role = aws_iam_role.scheduler_invoke.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.tle_fetcher.arn
    }]
  })
}

resource "aws_scheduler_schedule" "tle_fetcher" {
  name                         = "sattrack-tle-fetcher-schedule"
  schedule_expression          = "rate(2 hours)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.tle_fetcher.arn
    role_arn = aws_iam_role.scheduler_invoke.arn
  }
}
