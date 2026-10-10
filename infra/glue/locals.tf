# Names and ARNs are built from fixed names, the account and the region, so the IAM policy is known
# at plan time (PLAN.md R1). The bundle is built by scripts/build_glue_bundle.py before any plan.
locals {
  account_id = var.expected_account_id

  name         = "ecp-glue-shape"
  bucket       = "ecp-glue-${local.account_id}-${var.region}"
  log_group    = "/aws-glue/ecp-shape"
  boundary_arn = "arn:aws:iam::${local.account_id}:policy/ecp-workload-boundary"

  script_key = "glue/shape_job.py"
  bundle_key = "glue/energy_curves_m5.zip"
  output_url = "s3://${local.bucket}/m5/shape"

  # Both files are in the repository (the script) or built from it (the bundle); a missing bundle
  # fails the plan here, with the command that makes it.
  script_path = "${path.module}/../../jobs/glue/shape_job.py"
  bundle_path = "${path.module}/build/energy_curves_m5.zip"

  # The template placeholders; tests/test_glue_iam.py checks every template uses only these.
  policy_values = { account_id = local.account_id, region = var.region }
}
