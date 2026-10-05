# ADR-0021 phase 1b: SCPs on the Workloads OU. SCPs are not enabled on the organization root
# (verified 2026-10-05: the root lists no policy types), so the organization is imported and the
# SCP type enabled. Enabling it attaches FullAWSAccess to the root, every OU and every account,
# which changes no permission; SCPs never restrict the management account.
import {
  to = aws_organizations_organization.this
  id = data.aws_organizations_organization.this.id
}

resource "aws_organizations_organization" "this" {
  feature_set                   = "ALL"
  aws_service_access_principals = ["sso.amazonaws.com"]
  enabled_policy_types          = ["SERVICE_CONTROL_POLICY"]

  lifecycle {
    prevent_destroy = true
    # Trusted service access (Identity Center today; IAM in phase 1c) is managed elsewhere, so
    # this stack can never remove an integration another project relies on.
    ignore_changes = [aws_service_access_principals]
  }
}

# Region allow-list, no leaving the organization, no long-term root user (AssumeRoot is kept).
resource "aws_organizations_policy" "workloads_baseline" {
  name        = "ecp-workloads-baseline"
  description = "ADR-0021: region allow-list, no LeaveOrganization, no long-term root user"
  type        = "SERVICE_CONTROL_POLICY"
  content     = jsonencode(jsondecode(file("${path.module}/policies/scp-workloads-baseline.json")))

  depends_on = [aws_organizations_organization.this]
}

# The break-glass role and the R2 workload boundary can only be changed by the Identity Center
# admin, and no permissions boundary can be removed.
resource "aws_organizations_policy" "workloads_protect" {
  name        = "ecp-workloads-protect"
  description = "ADR-0021: break-glass role lock and the second R2 layer"
  type        = "SERVICE_CONTROL_POLICY"
  content     = jsonencode(jsondecode(file("${path.module}/policies/scp-workloads-protect.json")))

  depends_on = [aws_organizations_organization.this]
}

resource "aws_organizations_policy_attachment" "workloads_baseline" {
  policy_id = aws_organizations_policy.workloads_baseline.id
  target_id = aws_organizations_organizational_unit.workloads.id
}

resource "aws_organizations_policy_attachment" "workloads_protect" {
  policy_id = aws_organizations_policy.workloads_protect.id
  target_id = aws_organizations_organizational_unit.workloads.id
}
