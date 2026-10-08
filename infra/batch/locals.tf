# Names and ARNs are built from fixed names, the account and the region, so every IAM policy is
# known at plan time in both stages (PLAN.md R1) and stage 2 changes no IAM (ADR-0022).
locals {
  account_id = var.expected_account_id
  stage2     = var.image_digest != ""

  name           = "ecp-batch"
  bucket         = "ecp-data-${local.account_id}-${var.region}"
  store_url      = "s3://${local.bucket}/store" # ADR-0019 store; staging/ lives under it
  function_name  = "ecp-batch-stage"
  task_family    = "ecp-batch-pipeline"
  container_name = "pipeline"
  boundary_arn   = "arn:aws:iam::${local.account_id}:policy/ecp-workload-boundary"

  # The template placeholders; tests/test_batch_iam.py checks every template uses only these.
  policy_values = { account_id = local.account_id, region = var.region }

  image_uri = "${aws_ecr_repository.batch.repository_url}@${var.image_digest}"
}
