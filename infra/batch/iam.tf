# The five workload roles, all in stage 1 (ADR-0022). Each carries the workload boundary
# (PLAN.md R2, ADR-0018); its trust and inline policy are the reviewed templates in policies/,
# which tests/test_batch_iam.py evaluates against the boundary offline.
#   stage     the Lambda: write staging/ only
#   task      the pipeline task: read and publish the store
#   exec      ECS's task execution role: pull the image, write the task's logs
#   sfn       the state machine: invoke, run the task, pass the two task roles to ECS
#   scheduler the schedule: start the state machine
locals {
  roles = toset(["stage", "task", "exec", "sfn", "scheduler"])
}

resource "aws_iam_role" "batch" {
  for_each = local.roles

  name                 = "${local.name}-${each.key}"
  assume_role_policy   = jsonencode(jsondecode(templatefile("${path.module}/policies/${each.key}-trust.json.tftpl", local.policy_values)))
  permissions_boundary = local.boundary_arn
}

resource "aws_iam_role_policy" "batch" {
  for_each = local.roles

  name   = "${local.name}-${each.key}"
  role   = aws_iam_role.batch[each.key].id
  policy = jsonencode(jsondecode(templatefile("${path.module}/policies/${each.key}-policy.json.tftpl", local.policy_values)))
}

# ECS would create this itself on CreateCluster (ECS Developer Guide, "Using service-linked roles
# for Amazon ECS"). Declared so it is in the plan and the stage-1 check; the cluster depends on it
# so CreateCluster cannot create it first and fail this create. A service-linked role cannot carry
# a boundary; the R2 rule covers aws_iam_role only.
resource "aws_iam_service_linked_role" "ecs" {
  aws_service_name = "ecs.amazonaws.com"
}
