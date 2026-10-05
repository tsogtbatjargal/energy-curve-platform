data "aws_caller_identity" "current" {}

# The state bucket this stack created is owned by infra/org since ADR-0021 phase 1a
# (moved_to_org.tf). Policies here still name it, by an ARN built from its name, which renders
# the same policy documents as the old resource reference did.
locals {
  account_id       = data.aws_caller_identity.current.account_id
  state_bucket     = "ecp-tfstate-${local.account_id}-${var.region}"
  state_bucket_arn = "arn:aws:s3:::${local.state_bucket}"
}
