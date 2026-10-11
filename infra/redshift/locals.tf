# Names and ARNs are built from fixed names, the account and the region, so the IAM policies are
# known at plan time (PLAN.md R1). Nothing here reads another stack's state: the two buckets the
# COPY role may read are named by the same constants those stacks use.
locals {
  account_id   = var.expected_account_id
  boundary_arn = "arn:aws:iam::${local.account_id}:policy/ecp-workload-boundary"

  name        = "ecp-redshift"
  database    = "ecp"
  copy_role   = "ecp-redshift-copy"
  caller_role = "ecp-redshift-data-api"

  # Redshift Serverless needs three subnets in three zones; sorted so the choice is stable.
  zones = slice(sort(data.aws_availability_zones.available.names), 0, min(3, length(data.aws_availability_zones.available.names)))

  # a, b, c -> 10.43.0.0/24, 10.43.1.0/24, 10.43.2.0/24 (the batch stack's VPC is 10.42.0.0/16).
  subnets = { for i, az in local.zones : ["a", "b", "c"][i] => { cidr = "10.43.${i}.0/24", az = az } }

  # The template placeholders; tests/test_redshift_iam.py checks every template uses only these.
  policy_values = { account_id = local.account_id, region = var.region }
}
