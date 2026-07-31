# -----------------------------------------------
# Phase 2 — HTTP API in front of the position Lambda
#
# HTTP API (apigatewayv2), not REST: cheaper and simpler, and nothing here
# needs REST-only extras like usage plans or request models.
# -----------------------------------------------

resource "aws_apigatewayv2_api" "sattrack" {
  name          = "${local.name_prefix}-api"
  protocol_type = "HTTP"

  cors_configuration {
    # Wide-open origins until Phase 3 exists — tighten to the CloudFront
    # domain once the globe is deployed.
    allow_origins = ["*"]
    allow_methods = ["GET"]
    allow_headers = ["Content-Type"]
    max_age       = 3600
  }

  tags = {
    Name = "${local.name_prefix}-api"
  }
}

resource "aws_apigatewayv2_integration" "api_lambda" {
  api_id                 = aws_apigatewayv2_api.sattrack.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "api" {
  # checkov:skip=CKV_AWS_309: deliberate design — this is a public read-only
  #   API for a public satellite tracker; there's no user/principal to
  #   authenticate, and the throttle below is the actual abuse control.
  for_each = local.api_routes

  api_id    = aws_apigatewayv2_api.sattrack.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"
}

resource "aws_cloudwatch_log_group" "api_gateway" {
  name              = "/aws/apigateway/${local.name_prefix}-api"
  retention_in_days = 14
}

# HTTP APIs (apigatewayv2) deliver access logs via a resource policy on the
# destination log group, not the account-level "CloudWatch Logs role ARN"
# that REST APIs (v1) use — without this, the stage update fails (or logs
# silently never arrive).
resource "aws_cloudwatch_log_resource_policy" "api_gateway" {
  policy_name = "${local.name_prefix}-api-gateway-logs"

  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "apigateway.amazonaws.com" }
      Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource  = "${aws_cloudwatch_log_group.api_gateway.arn}:*"
    }]
  })
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.sattrack.id
  name        = "$default"
  auto_deploy = true

  # Modest throttle: this API serves one hobbyist globe, not the public.
  # Caps the blast radius (and Lambda bill) if the URL leaks.
  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      routeKey       = "$context.routeKey"
      status         = "$context.status"
      responseLength = "$context.responseLength"
      integrationErr = "$context.integrationErrorMessage"
    })
  }

  tags = {
    Name = "${local.name_prefix}-api-default"
  }

  depends_on = [aws_cloudwatch_log_resource_policy.api_gateway]
}

resource "aws_lambda_permission" "apigw_invoke_api" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.sattrack.execution_arn}/*/*"
}
