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

  tags = {
    Name = "sattrack"
  }
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
  function_name    = "sattrack-tle-fetcher"
  role             = aws_iam_role.tle_fetcher.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 128
  filename         = data.archive_file.tle_fetcher.output_path
  source_code_hash = data.archive_file.tle_fetcher.output_base64sha256

  environment {
    variables = {
      TABLE_NAME      = aws_dynamodb_table.sattrack.name
      BUCKET_NAME     = aws_s3_bucket.tle_archive.bucket
      CELESTRAK_GROUP = "stations"
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
