# ADR-0021 phase 1b: the workload account. CreateAccount takes no parent, so AWS creates it under
# the organization root; once creation succeeds, the provider moves it into the Workloads OU
# (MoveAccount). Until that move it has only FullAWSAccess: the bootstrap window, its acceptance
# and its failure recovery are in ADR-0021. depends_on makes both SCPs attached before creation.
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
