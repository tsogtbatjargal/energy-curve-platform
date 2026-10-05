# PLAN.md R2 (ADR-0018): the most any role the deploy role creates may ever do. The deploy role
# can create ecp-* roles only with this boundary (policies/deploy-iam.json.tftpl), and can never
# change it; bootstrap is applied by the Identity Center admin, not by CI.

locals {
  workload_boundary_name = "ecp-workload-boundary"
  # Built from the name rather than the resource, so the deploy policy and the workload stacks
  # know it at plan time. The postcondition below checks it matches the real ARN.
  workload_boundary_arn = "arn:aws:iam::${local.account_id}:policy/${local.workload_boundary_name}"
}

resource "aws_iam_policy" "workload_boundary" {
  name        = local.workload_boundary_name
  description = "Permissions boundary for every role the ecp-gha-deploy role creates (PLAN.md R2)"
  policy = jsonencode(jsondecode(templatefile("${path.module}/policies/workload-boundary.json.tftpl", {
    account_id       = local.account_id
    state_bucket_arn = local.state_bucket_arn
  })))

  lifecycle {
    postcondition {
      condition     = self.arn == local.workload_boundary_arn
      error_message = "The workload boundary ARN differs from the one the deploy policy names."
    }
  }
}
