# ADR-0021 phase 1b: the workload account, created directly in the Workloads OU after both SCPs
# are attached there, so it is never outside the guardrails.
resource "aws_organizations_account" "workloads" {
  name                       = "ecp-workloads"
  email                      = var.workload_account_email
  parent_id                  = aws_organizations_organizational_unit.workloads.id
  role_name                  = "OrganizationAccountAccessRole"
  iam_user_access_to_billing = "ALLOW"
  close_on_deletion          = false

  lifecycle {
    prevent_destroy = true
    # Organizations cannot read role_name back after creation.
    ignore_changes = [role_name]
  }

  depends_on = [
    aws_organizations_policy_attachment.workloads_baseline,
    aws_organizations_policy_attachment.workloads_protect,
  ]
}
