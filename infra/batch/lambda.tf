# Stage 2: the guard. The plan fails unless the approved digest is in this stack's repository.
data "aws_ecr_image" "batch" {
  count = local.stage2 ? 1 : 0

  repository_name = aws_ecr_repository.batch.name
  image_digest    = var.image_digest
}

# The staging step (ADR-0020). No VPC. No reserved concurrency: the account's limit is 10 and AWS
# keeps 10 unreserved, so any reservation fails; the state machine is the only caller (ADR-0022).
# X-Ray tracing is off: a cost for a once-a-day function.
resource "aws_lambda_function" "stage" {
  count = local.stage2 ? 1 : 0

  function_name = local.function_name
  role          = aws_iam_role.batch["stage"].arn
  package_type  = "Image"
  image_uri     = local.image_uri
  architectures = ["x86_64"]
  memory_size   = 512
  timeout       = 60

  environment {
    variables = {
      ECP_SOURCE    = "synthetic"
      ECP_STORE_URL = local.store_url
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda.name
  }

  depends_on = [data.aws_ecr_image.batch, aws_ecr_repository_policy.batch]
}
