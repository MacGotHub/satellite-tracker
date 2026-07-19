# -----------------------------------------------
# Phase 4 — pass alerts
#
# One Lambda, two EventBridge schedules distinguished by input payload:
# a 10-minute "imminent" tick (~15-min heads-up per visible pass) and a
# daily 5 PM ET "digest" that only emails when good passes are coming.
#
# Delivery starts as an email subscription, added out-of-band so the
# address never lands in the repo or the state file:
#   aws sns subscribe --topic-arn <alerts_topic_arn output> \
#     --protocol email --notification-endpoint <address>
# SMS joins the same topic later, once a toll-free origination number is
# registered (US SNS SMS requirement).
# -----------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${local.name_prefix}-alerts"

  tags = {
    Name = "${local.name_prefix}-alerts"
  }
}

data "archive_file" "alerts" {
  type        = "zip"
  output_path = "${path.module}/build/alerts.zip"

  source {
    content  = file("${path.module}/../src/alerts/handler.py")
    filename = "alerts/handler.py"
  }

  source {
    content  = file("${path.module}/../src/shared/passes.py")
    filename = "shared/passes.py"
  }
}

resource "aws_iam_role" "alerts" {
  name = "${local.name_prefix}-alerts"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "alerts_logs" {
  role       = aws_iam_role.alerts.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# GetItem for watchlist TLEs + PutItem for dedupe flags (no Scan — the
# watchlist is explicit keys), publish to the alerts topic only, and read
# the observer-coordinates SecureString (default aws/ssm key, so no
# explicit kms:Decrypt statement is needed).
resource "aws_iam_role_policy" "alerts" {
  name = "${local.name_prefix}-alerts-access"
  role = aws_iam_role.alerts.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.sattrack.arn
      },
      {
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.alerts.arn
      },
      {
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:us-east-1:${data.aws_caller_identity.current.account_id}:parameter${local.observer_ssm_parameter}"
      }
    ]
  })
}

resource "aws_lambda_function" "alerts" {
  function_name    = "${local.name_prefix}-alerts"
  role             = aws_iam_role.alerts.arn
  handler          = "alerts.handler.handler"
  runtime          = "python3.12"
  timeout          = 60
  memory_size      = 512 # numpy/skyfield import speed scales with memory-allocated CPU
  filename         = data.archive_file.alerts.output_path
  source_code_hash = data.archive_file.alerts.output_base64sha256
  layers           = [aws_lambda_layer_version.skyfield.arn]

  environment {
    variables = {
      TABLE_NAME             = aws_dynamodb_table.sattrack.name
      TOPIC_ARN              = aws_sns_topic.alerts.arn
      OBSERVER_PARAM         = local.observer_ssm_parameter
      WATCHLIST              = join(",", local.alert_watchlist)
      MIN_PEAK_ELEVATION_DEG = tostring(local.alert_min_peak_elevation_deg)
      LEAD_MINUTES           = tostring(local.alert_lead_minutes)
      DIGEST_LOOKAHEAD_HOURS = tostring(local.digest_lookahead_hours)
    }
  }

  tags = {
    Name = "${local.name_prefix}-alerts"
  }
}

resource "aws_cloudwatch_log_group" "alerts" {
  name              = "/aws/lambda/${aws_lambda_function.alerts.function_name}"
  retention_in_days = 14
}

resource "aws_iam_role" "alerts_scheduler" {
  name = "${local.name_prefix}-alerts-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "alerts_scheduler" {
  name = "${local.name_prefix}-alerts-scheduler-invoke"
  role = aws_iam_role.alerts_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.alerts.arn
    }]
  })
}

# Both schedules hit the same function; the input payload picks the mode.
resource "aws_scheduler_schedule" "alerts" {
  for_each = {
    imminent = {
      expression = "rate(${local.alert_tick_minutes} minutes)"
      timezone   = "UTC"
    }
    digest = {
      expression = local.digest_schedule_cron
      timezone   = "America/New_York" # digest lands at 5 PM local year-round
    }
  }

  name                         = "${local.name_prefix}-alerts-${each.key}"
  schedule_expression          = each.value.expression
  schedule_expression_timezone = each.value.timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.alerts.arn
    role_arn = aws_iam_role.alerts_scheduler.arn
    input    = jsonencode({ mode = each.key })
  }
}
