# -----------------------------------------------
# Cost/volume-spike detection via the shared abuse-alarm module. This
# project had no CloudWatch alarms at all -- the only cost signal was a
# monthly budget, which (per the module's own origin story) finds a
# runaway days late. These two alarms fire in minutes on the two things
# here that could actually run away.
#
# Separate SNS topic from aws_sns_topic.alerts: that one carries
# user-facing "a satellite is passing overhead" notifications, not ops.
# The module's topic is unencrypted by default -- CloudWatch alarms can't
# publish through an alias/aws/sns-encrypted topic.
# -----------------------------------------------

module "abuse_alarm" {
  # checkov:skip=CKV_TF_1: private registry source pinned by the `version`
  # constraint below. CKV_TF_1 only recognises a git source at a commit
  # SHA; registry version pinning is the equivalent guarantee.
  source  = "app.terraform.io/macgothub/abuse-alarm/aws"
  version = "~> 0.5.0"

  name = local.name_prefix

  # No tags argument: provider default_tags already applies common_tags to
  # every resource here, the module's included.

  alarms = {
    ddb-write-runaway = {
      namespace          = "AWS/DynamoDB"
      metric_name        = "ConsumedWriteCapacityUnits"
      dimensions         = { TableName = aws_dynamodb_table.sattrack.name }
      statistic          = "Sum"
      period             = 300
      evaluation_periods = 2
      # tle_fetcher writes ~11k catalog items every 2h in one batched
      # burst (~11k WCU, 1-2 five-minute windows). Two *consecutive*
      # windows over 50k means the schedule broke or a write loop opened
      # up -- a single retry burst won't sustain across two windows.
      threshold = 50000
    }

    tle-fetcher-invocation-flood = {
      namespace   = "AWS/Lambda"
      metric_name = "Invocations"
      dimensions  = { FunctionName = aws_lambda_function.tle_fetcher.function_name }
      # Scheduled every 2h -> at most 1 invocation per 5-minute window.
      # 5 in a window means the EventBridge schedule is misconfigured or
      # the function is somehow invoking itself.
      threshold = 5
    }
  }
}

output "abuse_alarm_topic_arn" {
  value       = module.abuse_alarm.sns_topic_arn
  description = <<-EOT
    Cost/volume-spike alarm topic. Subscribe a real endpoint:
      aws sns subscribe --topic-arn <this> --protocol email \
        --notification-endpoint you@example.com
    then force one alarm to ALARM and confirm the email lands.
  EOT
}
