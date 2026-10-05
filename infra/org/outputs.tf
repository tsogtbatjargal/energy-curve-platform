output "workloads_ou_id" {
  value = aws_organizations_organizational_unit.workloads.id
}

output "workload_account_id" {
  value = aws_organizations_account.workloads.id
}
