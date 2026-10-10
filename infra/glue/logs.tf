# The job's continuous log (14 days, as the batch stack). Glue also writes its own default groups
# (/aws-glue/jobs/output and /error), which it creates itself and Terraform does not manage.
# CloudWatch Logs encrypts at rest; a KMS key adds cost for synthetic-data logs.
#trivy:ignore:AWS-0017
resource "aws_cloudwatch_log_group" "glue" {
  name              = local.log_group
  retention_in_days = 14
}
