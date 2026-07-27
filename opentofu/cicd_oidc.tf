# -----------------------------------------------
# Phase 5 — GitHub Actions CI/CD via OIDC
#
# No static AWS keys in GitHub secrets. GitHub Actions presents a
# short-lived signed OIDC token; AWS trades it for temporary STS
# credentials scoped to one of the two roles below. Two roles, not one,
# so a PR from any branch can only ever get read-only access — write
# access requires the token to additionally prove `ref = refs/heads/main`,
# which only a push to main (post-merge) can produce.
#
# Pre-flight check (2026-07-25): `aws iam list-open-id-connect-providers`
# returned empty for this account — no past-lab provider to reuse, so this
# creates one directly instead of referencing one via a data source.
# -----------------------------------------------

# GitHub rotates the TLS cert on token.actions.githubusercontent.com
# periodically (it did in 2023, breaking every hardcoded-thumbprint setup
# industry-wide) — fetch it live instead of pasting a thumbprint that will
# eventually go stale.
data "tls_certificate" "github_actions" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github_actions" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github_actions.certificates[length(data.tls_certificate.github_actions.certificates) - 1].sha1_fingerprint]
}

# -----------------------------------------------
# Roles
# -----------------------------------------------

# Assumed by the PR-triggered plan workflow. Repo-scoped but NOT
# branch-scoped — StringLike with a wildcard suffix so it matches both
# `...:pull_request` and `...:ref:refs/heads/<any-branch>`. Safe to be
# this loose only because this role is read-only below.
resource "aws_iam_role" "gha_plan" {
  name = "${local.name_prefix}-gha-plan"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github_actions.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "${local.github_oidc_sub_prefix}:*"
        }
      }
    }]
  })
}

# Assumed by the push-to-main apply workflow only. Pinned to exactly one
# ref via StringLike (no wildcard is needed in the ref segment itself, but
# StringLike vs. StringEquals doesn't matter here — the whole suffix is
# fixed). This is the trust-policy line DESIGN.md calls out: a wildcard
# `sub` here would let a PR from a fork assume a role that can change
# live infrastructure.
resource "aws_iam_role" "gha_apply" {
  name = "${local.name_prefix}-gha-apply"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github_actions.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "${local.github_oidc_sub_prefix}:ref:refs/heads/main"
        }
      }
    }]
  })
}

# -----------------------------------------------
# Permissions — read policy (both roles: plan needs it to compute a diff,
# apply needs it too since apply always plans first). Scoped to the exact
# resources this project already owns; growing the resource set in a
# future phase means growing this policy in the same PR, on purpose —
# scope drift should be a reviewed diff, not silent.
# -----------------------------------------------

resource "aws_iam_policy" "gha_read" {
  name = "${local.name_prefix}-gha-read"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CallerIdentity"
        Effect   = "Allow"
        Action   = "sts:GetCallerIdentity"
        Resource = "*" # no resource-level permission model exists for this action
      },
      {
        Sid      = "StateBucketList"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = "arn:aws:s3:::351668480009-opentofu-state"
        Condition = {
          StringLike = { "s3:prefix" = ["sattrack/tle-pipeline/*"] }
        }
      },
      {
        Sid      = "StateObjectRead"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "arn:aws:s3:::351668480009-opentofu-state/sattrack/tle-pipeline/*"
      },
      {
        Sid      = "StateLock"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = "arn:aws:dynamodb:us-east-1:${data.aws_caller_identity.current.account_id}:table/opentofu-state-lock"
      },
      {
        Sid      = "DynamoDbRead"
        Effect   = "Allow"
        Action   = ["dynamodb:DescribeTable", "dynamodb:DescribeTimeToLive", "dynamodb:DescribeContinuousBackups", "dynamodb:ListTagsOfResource"]
        Resource = aws_dynamodb_table.sattrack.arn
      },
      {
        Sid    = "S3BucketRead"
        Effect = "Allow"
        Action = [
          "s3:GetBucketPolicy", "s3:GetBucketPublicAccessBlock", "s3:GetBucketTagging",
          "s3:GetBucketAcl", "s3:GetEncryptionConfiguration", "s3:GetBucketCors",
          "s3:GetBucketWebsite", "s3:GetBucketVersioning", "s3:ListBucket"
        ]
        Resource = [aws_s3_bucket.tle_archive.arn, aws_s3_bucket.frontend.arn]
      },
      {
        # The AWS provider refreshes this resource's own state on every
        # plan, same as anything else this module manages.
        Sid      = "OidcProviderRead"
        Effect   = "Allow"
        Action   = "iam:GetOpenIDConnectProvider"
        Resource = aws_iam_openid_connect_provider.github_actions.arn
      },
      {
        Sid      = "S3ObjectRead"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectTagging"]
        Resource = ["${aws_s3_bucket.tle_archive.arn}/*", "${aws_s3_bucket.frontend.arn}/*"]
      },
      {
        Sid    = "LambdaRead"
        Effect = "Allow"
        Action = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:GetFunctionCodeSigningConfig", "lambda:ListVersionsByFunction", "lambda:GetPolicy", "lambda:ListTags"]
        Resource = [
          aws_lambda_function.tle_fetcher.arn,
          aws_lambda_function.api.arn,
          aws_lambda_function.alerts.arn,
        ]
      },
      {
        Sid      = "LambdaLayerRead"
        Effect   = "Allow"
        Action   = ["lambda:GetLayerVersion", "lambda:ListLayerVersions"]
        Resource = "${aws_lambda_layer_version.skyfield.layer_arn}:*"
      },
      {
        Sid      = "IamRoleRead"
        Effect   = "Allow"
        Action   = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListRoleTags"]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-*"
      },
      {
        Sid      = "SnsRead"
        Effect   = "Allow"
        Action   = ["sns:GetTopicAttributes", "sns:ListTagsForResource", "sns:ListSubscriptionsByTopic"]
        Resource = aws_sns_topic.alerts.arn
      },
      {
        Sid    = "SchedulerRead"
        Effect = "Allow"
        Action = ["scheduler:GetSchedule", "scheduler:ListTagsForResource"]
        Resource = [
          aws_scheduler_schedule.tle_fetcher.arn,
          "arn:aws:scheduler:us-east-1:${data.aws_caller_identity.current.account_id}:schedule/default/${local.name_prefix}-alerts-*",
        ]
      },
      {
        # CloudWatch Logs ARNs need the trailing :* (log-stream segment) —
        # without it, DescribeLogGroups rejects the resource match entirely.
        Sid    = "LogsRead"
        Effect = "Allow"
        Action = ["logs:DescribeLogGroups", "logs:ListTagsLogGroup"]
        Resource = [
          "arn:aws:logs:us-east-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.name_prefix}-*",
          "arn:aws:logs:us-east-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.name_prefix}-*:*",
        ]
      },
      {
        Sid      = "ApiGatewayRead"
        Effect   = "Allow"
        Action   = "apigateway:GET"
        Resource = "arn:aws:apigateway:us-east-1::/apis/${aws_apigatewayv2_api.sattrack.id}*"
      },
      {
        # CloudFront has no resource-level IAM support for these actions —
        # AWS requires Resource "*" regardless of which distribution.
        Sid      = "CloudFrontRead"
        Effect   = "Allow"
        Action   = ["cloudfront:GetDistribution", "cloudfront:GetDistributionConfig", "cloudfront:ListTagsForResource", "cloudfront:GetCachePolicy", "cloudfront:GetOriginAccessControl"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "gha_plan_read" {
  role       = aws_iam_role.gha_plan.name
  policy_arn = aws_iam_policy.gha_read.arn
}

resource "aws_iam_role_policy_attachment" "gha_apply_read" {
  role       = aws_iam_role.gha_apply.name
  policy_arn = aws_iam_policy.gha_read.arn
}

# -----------------------------------------------
# Permissions — write policy (apply role only)
# -----------------------------------------------

resource "aws_iam_policy" "gha_write" {
  name = "${local.name_prefix}-gha-write"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DynamoDbWrite"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateTable", "dynamodb:UpdateTimeToLive", "dynamodb:TagResource", "dynamodb:UntagResource"]
        Resource = aws_dynamodb_table.sattrack.arn
      },
      {
        Sid    = "S3BucketWrite"
        Effect = "Allow"
        Action = [
          "s3:PutBucketPolicy", "s3:PutBucketPublicAccessBlock", "s3:PutBucketTagging",
          "s3:PutEncryptionConfiguration"
        ]
        Resource = [aws_s3_bucket.tle_archive.arn, aws_s3_bucket.frontend.arn]
      },
      {
        Sid      = "S3ObjectWrite"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:DeleteObject", "s3:PutObjectTagging"]
        Resource = ["${aws_s3_bucket.tle_archive.arn}/*", "${aws_s3_bucket.frontend.arn}/*"]
      },
      {
        Sid    = "LambdaWrite"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
          "lambda:DeleteFunction", "lambda:AddPermission", "lambda:RemovePermission", "lambda:TagResource", "lambda:UntagResource"
        ]
        Resource = "arn:aws:lambda:us-east-1:${data.aws_caller_identity.current.account_id}:function:${local.name_prefix}-*"
      },
      {
        Sid      = "LambdaLayerWrite"
        Effect   = "Allow"
        Action   = ["lambda:PublishLayerVersion", "lambda:DeleteLayerVersion"]
        Resource = "${aws_lambda_layer_version.skyfield.layer_arn}*"
      },
      {
        Sid    = "IamRoleWrite"
        Effect = "Allow"
        Action = [
          "iam:CreateRole", "iam:DeleteRole", "iam:UpdateRole",
          "iam:PutRolePolicy", "iam:DeleteRolePolicy",
          "iam:AttachRolePolicy", "iam:DetachRolePolicy",
          "iam:TagRole", "iam:UntagRole"
        ]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-*"
      },
      {
        # PassRole is the classic IAM privilege-escalation vector — restrict
        # it to exactly the AWS services that ever assume a sattrack-* role.
        Sid      = "PassRoleToOwnServices"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-*"
        Condition = {
          StringEquals = {
            "iam:PassedToService" = ["lambda.amazonaws.com", "scheduler.amazonaws.com"]
          }
        }
      },
      {
        Sid      = "SnsWrite"
        Effect   = "Allow"
        Action   = ["sns:SetTopicAttributes", "sns:TagResource", "sns:UntagResource"]
        Resource = aws_sns_topic.alerts.arn
      },
      {
        Sid    = "SchedulerWrite"
        Effect = "Allow"
        Action = ["scheduler:UpdateSchedule", "scheduler:TagResource", "scheduler:UntagResource"]
        Resource = [
          aws_scheduler_schedule.tle_fetcher.arn,
          "arn:aws:scheduler:us-east-1:${data.aws_caller_identity.current.account_id}:schedule/default/${local.name_prefix}-alerts-*",
        ]
      },
      {
        Sid    = "LogsWrite"
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:PutRetentionPolicy", "logs:TagLogGroup"]
        Resource = [
          "arn:aws:logs:us-east-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.name_prefix}-*",
          "arn:aws:logs:us-east-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.name_prefix}-*:*",
        ]
      },
      {
        # apigatewayv2's IAM model is action-on-path, not action-on-name —
        # the API ID is a system-generated token with no naming pattern to
        # scope by, so nested writes are scoped to the one API this project
        # owns; only the top-level create verb needs the broader /apis path.
        Sid      = "ApiGatewayWriteExisting"
        Effect   = "Allow"
        Action   = ["apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE"]
        Resource = "arn:aws:apigateway:us-east-1::/apis/${aws_apigatewayv2_api.sattrack.id}*"
      },
      {
        Sid      = "ApiGatewayCreateTopLevel"
        Effect   = "Allow"
        Action   = "apigateway:POST"
        Resource = "arn:aws:apigateway:us-east-1::/apis"
      },
      {
        # CloudFront: same story as the read statement above — no
        # resource-level IAM support, Resource "*" is the only option.
        Sid    = "CloudFrontWrite"
        Effect = "Allow"
        Action = [
          "cloudfront:UpdateDistribution", "cloudfront:CreateCachePolicy", "cloudfront:UpdateCachePolicy",
          "cloudfront:DeleteCachePolicy", "cloudfront:CreateOriginAccessControl", "cloudfront:UpdateOriginAccessControl",
          "cloudfront:DeleteOriginAccessControl", "cloudfront:TagResource", "cloudfront:UntagResource"
        ]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "gha_apply_write" {
  role       = aws_iam_role.gha_apply.name
  policy_arn = aws_iam_policy.gha_write.arn
}
