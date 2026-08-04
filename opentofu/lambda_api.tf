# -----------------------------------------------
# Phase 2 — position API Lambda
#
# Phase 6 Step 5 dropped this Lambda's Skyfield dependency entirely (see
# the handler's own docstring) — the layer resource stays defined here
# since it's still built from this phase's original zip, but it's no
# longer attached to aws_lambda_function.api below. Its only remaining
# consumer is aws_lambda_function.alerts in alerts.tf.
# -----------------------------------------------

resource "aws_lambda_layer_version" "skyfield" {
  layer_name          = "${local.name_prefix}-skyfield"
  description         = "skyfield + numpy + sgp4 + jplephem + de421.bsp (see src/layers/skyfield)"
  filename            = "${path.module}/../src/layers/skyfield/dist/skyfield-layer.zip"
  source_code_hash    = filebase64sha256("${path.module}/../src/layers/skyfield/dist/skyfield-layer.zip")
  compatible_runtimes = ["python3.12"]
}

# Explicit source block (not source_dir) so the zip contains exactly the
# handler — and never the layer artifacts or tle_fetch/shared code that
# also live under src/. shared/passes.py dropped in Step 5: this handler
# no longer imports it (see its docstring).
data "archive_file" "api" {
  type        = "zip"
  output_path = "${path.module}/build/api.zip"

  source {
    content  = file("${path.module}/../src/api/handler.py")
    filename = "api/handler.py"
  }
}

resource "aws_iam_role" "api" {
  name = "${local.name_prefix}-api"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "api_logs" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Read-only on the catalog — this Lambda serves data; only the Phase 1
# fetcher writes it. GetItem dropped in Step 5: it only backed the two
# per-item routes retired in that step, and _list_satellites is Scan-only.
resource "aws_iam_role_policy" "api" {
  name = "${local.name_prefix}-api-access"
  role = aws_iam_role.api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "dynamodb:Scan"
      Resource = aws_dynamodb_table.sattrack.arn
    }]
  })
}

resource "aws_lambda_function" "api" {
  function_name = "${local.name_prefix}-api"
  role          = aws_iam_role.api.arn
  handler       = "api.handler.handler"
  runtime       = "python3.12"
  # 15s -> 30s: safety margin for _list_satellites's Scan+serialize now
  # that Phase 6 Step 3 added Starlink (~10,800 items) to the catalog —
  # not evidence 15s actually failed, GET /positions (the Skyfield-heavy
  # route this would have really strained) was retired in the same step.
  timeout = 30
  # No longer a numpy/Skyfield import cost as of Step 5 — kept at 512
  # rather than re-tuned down, since it's Scan+JSON-serialize of the
  # ~10,800-item catalog (proven to fit the 30s timeout above at this
  # size) that sizes this now, not import time.
  memory_size      = 512
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.sattrack.name
    }
  }

  tags = {
    Name = "${local.name_prefix}-api"
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${aws_lambda_function.api.function_name}"
  retention_in_days = 14
}
