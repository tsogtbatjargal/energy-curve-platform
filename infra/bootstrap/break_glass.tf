# ADR-0021 phase 3, break-glass: Organizations created OrganizationAccountAccessRole trusting the
# whole management account. The member instance imports it and narrows the trust to the admin's
# Identity Center sessions, with a source identity required so CloudTrail records who assumed it.
# It keeps AdministratorAccess (not managed here). The Workloads protect SCP lets only those
# sessions change it, so the apply must run as the admin (ecp-workloads-admin).
locals {
  break_glass = var.member_instance ? toset(["OrganizationAccountAccessRole"]) : toset([])
}

import {
  for_each = local.break_glass
  to       = aws_iam_role.break_glass[each.key]
  id       = each.key
}

resource "aws_iam_role" "break_glass" {
  for_each = local.break_glass
  name     = each.key
  assume_role_policy = jsonencode(jsondecode(templatefile("${path.module}/policies/break-glass-trust.json.tftpl", {
    management_account_id = data.aws_organizations_organization.this.master_account_id
  })))
  max_session_duration = 3600

  lifecycle {
    prevent_destroy = true
  }
}
