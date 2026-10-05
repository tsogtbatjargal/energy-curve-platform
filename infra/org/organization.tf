data "aws_organizations_organization" "this" {}

# The OU the workload account joins in phase 1b, and where its SCPs attach (ADR-0021).
resource "aws_organizations_organizational_unit" "workloads" {
  name      = "Workloads"
  parent_id = data.aws_organizations_organization.this.roots[0].id
}
