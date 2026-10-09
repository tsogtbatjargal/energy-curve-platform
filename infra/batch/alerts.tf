# A failed, timed-out or aborted execution emails the user (PLAN.md: "a CloudWatch alarm fires on a
# failed run"). The address is set only in the git-ignored terraform.tfvars. The subscription stays
# "pending confirmation" until the user confirms it.
#
# Not encrypted: CloudWatch alarms cannot publish to a topic encrypted with the AWS-managed SNS key,
# and a customer-managed key is excluded by cost (ADR-0022). The topic carries alarm states only.
#trivy:ignore:AWS-0095
resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email

  # CI plans this stack with a placeholder address (the real one is private), and the endpoint
  # of an email subscription cannot change in place. Changing the address later is a deliberate
  # replace step: `terraform apply -replace=aws_sns_topic_subscription.email` (ADR-0022).
  lifecycle {
    ignore_changes = [endpoint]
  }
}

# Stage 2: one alarm over the three ways an execution can end badly.
resource "aws_cloudwatch_metric_alarm" "failures" {
  count = local.stage2 ? 1 : 0

  alarm_name          = "${local.name}-failures"
  alarm_description   = "An ecp-batch execution failed, timed out or was aborted."
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  metric_query {
    id          = "bad"
    expression  = "SUM([failed, timedout, aborted])"
    label       = "Executions that did not succeed"
    return_data = true
  }

  dynamic "metric_query" {
    for_each = { failed = "ExecutionsFailed", timedout = "ExecutionsTimedOut", aborted = "ExecutionsAborted" }
    content {
      id = metric_query.key
      metric {
        namespace   = "AWS/States"
        metric_name = metric_query.value
        period      = 300
        stat        = "Sum"
        dimensions  = { StateMachineArn = aws_sfn_state_machine.batch[0].arn }
      }
    }
  }
}
