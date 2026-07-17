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
  for_each = local.api_routes

  api_id    = aws_apigatewayv2_api.sattrack.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.api_lambda.id}"
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

  tags = {
    Name = "${local.name_prefix}-api-default"
  }
}

resource "aws_lambda_permission" "apigw_invoke_api" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.sattrack.execution_arn}/*/*"
}
