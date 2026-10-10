# The job's role (PLAN.md R2, ADR-0018): the workload boundary, a trust for the Glue service only,
# and an inline policy that is the reviewed template in policies/ (tests/test_glue_iam.py evaluates
# it against the boundary offline).
resource "aws_iam_role" "glue" {
  name                 = local.name
  assume_role_policy   = jsonencode(jsondecode(templatefile("${path.module}/policies/glue-trust.json.tftpl", local.policy_values)))
  permissions_boundary = local.boundary_arn
}

resource "aws_iam_role_policy" "glue" {
  name   = local.name
  role   = aws_iam_role.glue.id
  policy = jsonencode(jsondecode(templatefile("${path.module}/policies/glue-policy.json.tftpl", local.policy_values)))
}
