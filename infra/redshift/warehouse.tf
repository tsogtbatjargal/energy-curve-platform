# ADR-0023: Redshift Serverless on demand. Idle compute costs nothing; storage is under 1 GB.
#
# Capacity: ca-central-1 has no 4 RPU option (the documented list of Regions with 4 RPU does not
# include it; 8 RPU is the minimum there), so the base is 8 and the maximum is 8: no autoscaling,
# and $3.30 an hour while queries run. The default base capacity is 128 RPU, $52.80 an hour; the
# plan check and the Rego policy refuse a plan that leaves either value unset.
#
# IAM only: no admin_username, admin_user_password, admin_user_password_wo or manage_admin_password,
# so there is no password in state and no Secrets Manager secret. The caller's IAM identity maps to
# a database user through redshift-serverless:GetCredentials. No final snapshot is configurable
# (the provider calls DeleteNamespace with the name only), and the data is synthetic and
# reproducible from the repository.
resource "aws_redshiftserverless_namespace" "this" {
  namespace_name = local.name
  db_name        = local.database
  iam_roles      = [aws_iam_role.copy.arn]

  depends_on = [aws_iam_service_linked_role.redshift]
}

resource "aws_redshiftserverless_workgroup" "this" {
  workgroup_name       = local.name
  namespace_name       = aws_redshiftserverless_namespace.this.namespace_name
  base_capacity        = 8
  max_capacity         = 8
  publicly_accessible  = false
  enhanced_vpc_routing = false
  subnet_ids           = values(aws_subnet.private)[*].id
  security_group_ids   = [aws_security_group.redshift.id]

  depends_on = [aws_iam_service_linked_role.redshift]
}

# A backstop, not a cap: the RPU-hours limit "doesn't set a ceiling on capacity" and acts at a
# time AWS does not document. max_capacity is the ceiling on the rate (8 RPU, $3.30 an hour).
# 4 RPU-hours a day is 30 minutes of compute at 8 RPU.
resource "aws_redshiftserverless_usage_limit" "this" {
  resource_arn  = aws_redshiftserverless_workgroup.this.arn
  usage_type    = "serverless-compute"
  period        = "daily"
  amount        = 4
  breach_action = "deactivate"
}
