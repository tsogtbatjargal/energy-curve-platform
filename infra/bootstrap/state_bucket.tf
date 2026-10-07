data "aws_caller_identity" "current" {
  lifecycle {
    postcondition {
      condition     = self.account_id == var.expected_account_id
      error_message = "Wrong account for this instance: the credentials are not expected_account_id. Check the profile, the tfvars and the data directory."
    }
  }
}

# In the management account, the state bucket this stack created is owned by infra/org since
# ADR-0021 phase 1a (moved_to_org.tf). The member instance creates its own (member_state.tf), with
# the same naming. Policies here name the bucket by an ARN built from its name, which renders the
# same policy documents as the old resource reference did.
locals {
  account_id       = data.aws_caller_identity.current.account_id
  state_bucket     = "ecp-tfstate-${local.account_id}-${var.region}"
  state_bucket_arn = "arn:aws:s3:::${local.state_bucket}"
}
