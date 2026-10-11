# The job's logs, under one prefix (--custom-logGroup-prefix): Glue 5 writes the driver and Spark
# logs to <prefix>/error and stdout/stderr to <prefix>/output. It ignores the continuous-logging
# arguments of Glue 4 and earlier, which left /aws-glue/ecp-shape empty in the first two runs.
# 14 days, as the batch stack. Glue's own default groups (/aws-glue/jobs/*) are not managed here.
# CloudWatch Logs encrypts at rest; a KMS key adds cost for synthetic-data logs.
#trivy:ignore:AWS-0017
resource "aws_cloudwatch_log_group" "output" {
  name              = "${local.log_group_prefix}/output"
  retention_in_days = 14
}

#trivy:ignore:AWS-0017
resource "aws_cloudwatch_log_group" "error" {
  name              = "${local.log_group_prefix}/error"
  retention_in_days = 14
}
