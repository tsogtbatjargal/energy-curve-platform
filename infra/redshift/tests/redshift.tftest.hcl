# M5c-1 (ADR-0023): the Redshift stack planned and applied against a mocked AWS provider. Offline:
# no credentials, no backend, no AWS call. Account IDs are placeholders.
#   terraform -chdir=infra/redshift init -backend=false && terraform -chdir=infra/redshift test

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "333333333333" }
  }
  mock_data "aws_organizations_organization" {
    defaults = { master_account_id = "111111111111" }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["ca-central-1a", "ca-central-1b", "ca-central-1d"] }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::333333333333:role/ecp-redshift-copy" }
  }
  mock_resource "aws_redshiftserverless_workgroup" {
    defaults = { arn = "arn:aws:redshift-serverless:ca-central-1:333333333333:workgroup/00000000-0000-0000-0000-000000000000" }
  }
}

variables {
  expected_account_id = "333333333333"
}

run "the_warehouse_is_8_rpu_private_and_without_enhanced_vpc_routing" {
  command = apply

  assert {
    condition     = aws_redshiftserverless_workgroup.this.base_capacity == 8 && aws_redshiftserverless_workgroup.this.max_capacity == 8
    error_message = "8 RPU base and 8 RPU max: ca-central-1 has no 4 RPU option, and the default of 128 RPU costs $52.80 an hour."
  }
  assert {
    condition     = aws_redshiftserverless_workgroup.this.publicly_accessible == false && aws_redshiftserverless_workgroup.this.enhanced_vpc_routing == false
    error_message = "Not publicly accessible; enhanced VPC routing off (the fallback is a planned change, not a toggle)."
  }
  assert {
    condition     = length(aws_redshiftserverless_workgroup.this.subnet_ids) == 3 && length(aws_redshiftserverless_workgroup.this.security_group_ids) == 1
    error_message = "Three subnets and the one security group."
  }
}

run "the_namespace_is_iam_only_with_the_copy_role" {
  command = apply

  assert {
    condition     = aws_redshiftserverless_namespace.this.admin_user_password == null && aws_redshiftserverless_namespace.this.manage_admin_password == null
    error_message = "No admin password and no managed secret: the Data API uses IAM."
  }
  assert {
    condition     = aws_redshiftserverless_namespace.this.namespace_name == "ecp-redshift" && aws_redshiftserverless_namespace.this.db_name == "ecp" && length(aws_redshiftserverless_namespace.this.iam_roles) == 1
    error_message = "One namespace, one database, one associated role."
  }
}

run "the_limit_is_a_daily_backstop_that_deactivates" {
  command = apply

  assert {
    condition     = aws_redshiftserverless_usage_limit.this.usage_type == "serverless-compute" && aws_redshiftserverless_usage_limit.this.period == "daily" && aws_redshiftserverless_usage_limit.this.amount == 4 && aws_redshiftserverless_usage_limit.this.breach_action == "deactivate"
    error_message = "4 RPU-hours a day, then deactivate. A backstop that acts after the fact, not a cap; max_capacity is the ceiling."
  }
}

run "the_network_is_three_private_subnets_in_three_zones" {
  command = apply

  assert {
    condition     = length(aws_subnet.private) == 3 && length(toset([for s in aws_subnet.private : s.availability_zone])) == 3
    error_message = "Three subnets, three different Availability Zones."
  }
  assert {
    condition     = alltrue([for s in aws_subnet.private : s.map_public_ip_on_launch == false])
    error_message = "No public addresses."
  }
  assert {
    condition     = aws_vpc.redshift.cidr_block == "10.43.0.0/16"
    error_message = "Its own VPC, apart from the batch stack's 10.42.0.0/16."
  }
}

run "both_roles_carry_the_boundary" {
  command = apply

  assert {
    condition     = aws_iam_role.copy.name == "ecp-redshift-copy" && aws_iam_role.caller.name == "ecp-redshift-data-api"
    error_message = "The two roles are ecp-redshift-copy and ecp-redshift-data-api."
  }
  assert {
    condition     = aws_iam_role.copy.permissions_boundary == "arn:aws:iam::333333333333:policy/ecp-workload-boundary" && aws_iam_role.caller.permissions_boundary == "arn:aws:iam::333333333333:policy/ecp-workload-boundary"
    error_message = "Both carry the workload boundary (PLAN.md R2)."
  }
}

run "the_service_linked_role_is_declared" {
  command = apply

  assert {
    condition     = aws_iam_service_linked_role.redshift.aws_service_name == "redshift.amazonaws.com"
    error_message = "Declared, so it is in the plan and the plan check; the warehouse depends on it."
  }
}

run "the_wrong_region_is_refused" {
  command = plan
  variables {
    region = "us-east-1"
  }
  expect_failures = [var.region]
}

run "the_wrong_account_is_refused" {
  command = plan
  variables {
    expected_account_id = "444444444444"
  }
  expect_failures = [data.aws_caller_identity.current]
}

run "fewer_than_three_zones_stop_the_plan" {
  command = plan

  override_data {
    target = data.aws_availability_zones.available
    values = { names = ["ca-central-1a", "ca-central-1b"] }
  }

  expect_failures = [aws_vpc.redshift]
}
