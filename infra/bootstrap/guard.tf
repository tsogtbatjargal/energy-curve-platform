# ADR-0021 phase 3: the same stack runs in the management account and in ecp-workloads, from
# separate data directories and backend files. A plan with the wrong profile, tfvars or backend
# stops here, before any resource is planned: the caller must be expected_account_id, and
# member_instance must match whether that account is the organization's management account.
data "aws_organizations_organization" "this" {
  lifecycle {
    postcondition {
      condition     = (self.master_account_id != data.aws_caller_identity.current.account_id) == var.member_instance
      error_message = "member_instance does not match this account: true only in a member account, false only in the organization's management account."
    }
  }
}
