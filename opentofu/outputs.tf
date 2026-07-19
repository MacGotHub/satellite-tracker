output "dynamodb_table_name" {
  value = aws_dynamodb_table.sattrack.name
}

output "tle_archive_bucket" {
  value = aws_s3_bucket.tle_archive.bucket
}

output "tle_fetcher_function_name" {
  value = aws_lambda_function.tle_fetcher.function_name
}

output "api_endpoint" {
  value       = aws_apigatewayv2_api.sattrack.api_endpoint
  description = "Base URL for the position/pass API (Phase 2)"
}

output "globe_url" {
  value       = "https://${aws_cloudfront_distribution.frontend.domain_name}"
  description = "The 3D globe (Phase 3)"
}

output "alerts_topic_arn" {
  value       = aws_sns_topic.alerts.arn
  description = "Subscribe alert endpoints here (Phase 4) — see alerts.tf header for the command"
}
