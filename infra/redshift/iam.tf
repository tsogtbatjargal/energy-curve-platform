# Two roles (PLAN.md R2, ADR-0018), each with the workload boundary, a reviewed trust and a
# reviewed inline policy from policies/ (tests/test_redshift_iam.py evaluates them against the
# boundary offline).
#
# copy:   assumed by Redshift to read exactly the shape file and the three gold files for COPY.
# caller: assumed by the operator (this account's AdministratorAccess sessions) to run
#         statements through the Data API on this stack's workgroup. No password, no secret.
resource "aws_iam_role" "copy" {
  name                 = local.copy_role
  assume_role_policy   = jsonencode(jsondecode(templatefile("${path.module}/policies/copy-trust.json.tftpl", local.policy_values)))
  permissions_boundary = local.boundary_arn
}

resource "aws_iam_role_policy" "copy" {
  name   = local.copy_role
  role   = aws_iam_role.copy.id
  policy = jsonencode(jsondecode(templatefile("${path.module}/policies/copy-policy.json.tftpl", local.policy_values)))
}

resource "aws_iam_role" "caller" {
  name                 = local.caller_role
  assume_role_policy   = jsonencode(jsondecode(templatefile("${path.module}/policies/data-api-trust.json.tftpl", local.policy_values)))
  permissions_boundary = local.boundary_arn
}

resource "aws_iam_role_policy" "caller" {
  name   = local.caller_role
  role   = aws_iam_role.caller.id
  policy = jsonencode(jsondecode(templatefile("${path.module}/policies/data-api-policy.json.tftpl", local.policy_values)))
}

# Redshift creates AWSServiceRoleForRedshift itself the first time it is used in an account (Redshift
# Management Guide, "Using service-linked roles for Amazon Redshift"). This account has never used
# Redshift (no stack here names it before this one), so the role is declared, as the ECS one is
# (ADR-0022): it is in the plan and the plan check, and the warehouse depends on it so the service
# cannot create it first and fail this create. It cannot carry a boundary; R2's rule covers
# aws_iam_role only. If the role already exists the create fails with EntityAlreadyExists and the
# fix is an import, in its own reviewed change.
resource "aws_iam_service_linked_role" "redshift" {
  aws_service_name = "redshift.amazonaws.com"
}
