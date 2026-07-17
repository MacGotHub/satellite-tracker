# -----------------------------------------------
# Phase 2 — position/pass API Lambda
#
# Skyfield + numpy + the de421 ephemeris live in a Lambda layer (built by
# src/layers/skyfield/build.py — run it before plan if dist/ is missing).
# The function zip carries only the handler and the shared pass logic, so
# routine code changes don't republish 32 MB of dependencies.
# -----------------------------------------------

resource "aws_lambda_layer_version" "skyfield" {
  layer_name          = "${local.name_prefix}-skyfield"
  description         = "skyfield + numpy + sgp4 + jplephem + de421.bsp (see src/layers/skyfield)"
  filename            = "${path.module}/../src/layers/skyfield/dist/skyfield-layer.zip"
  source_code_hash    = filebase64sha256("${path.module}/../src/layers/skyfield/dist/skyfield-layer.zip")
  compatible_runtimes = ["python3.12"]
}

# Explicit source blocks (not source_dir) so the zip contains exactly the
# handler and shared/ — and never the layer artifacts or tle_fetch code
# that also live under src/.
data "archive_file" "api" {
  type        = "zip"
  output_path = "${path.module}/build/api.zip"

  source {
    content  = file("${path.module}/../src/api/handler.py")
    filename = "api/handler.py"
  }

  source {
    content  = file("${path.module}/../src/shared/passes.py")
    filename = "shared/passes.py"
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
# fetcher writes it.
resource "aws_iam_role_policy" "api" {
  name = "${local.name_prefix}-api-access"
  role = aws_iam_role.api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem", "dynamodb:Scan"]
      Resource = aws_dynamodb_table.sattrack.arn
    }]
  })
}

resource "aws_lambda_function" "api" {
  function_name    = "${local.name_prefix}-api"
  role             = aws_iam_role.api.arn
  handler          = "api.handler.handler"
  runtime          = "python3.12"
  timeout          = 15
  memory_size      = 512 # numpy import + pass search want headroom; 128 MB is painfully slow
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  layers           = [aws_lambda_layer_version.skyfield.arn]

  environment {
    variables = {
      TABLE_NAME     = aws_dynamodb_table.sattrack.name
      EPHEMERIS_PATH = "/opt/data/de421.bsp"
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
