output "state_bucket" {
  value = aws_s3_bucket.tfstate.bucket
}

output "gha_plan_role_arn" {
  value = aws_iam_role.gha_plan.arn
}

output "gha_deploy_role_arn" {
  value = aws_iam_role.gha_deploy.arn
}

# Workload stacks set permissions_boundary to this on every role (PLAN.md R2).
output "workload_boundary_arn" {
  value = aws_iam_policy.workload_boundary.arn
}
