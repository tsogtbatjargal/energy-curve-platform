# Stage 1, so the roles can name them and stage 2 only writes to them. 14 days (ADR-0022).
# CloudWatch Logs encrypts at rest; a KMS key adds cost for synthetic-data logs.
#trivy:ignore:AWS-0017
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 14
}

#trivy:ignore:AWS-0017
resource "aws_cloudwatch_log_group" "pipeline" {
  name              = "/ecs/${local.task_family}"
  retention_in_days = 14
}
