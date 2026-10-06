# ADR-0021 phase 1c: Identity Center access to the workload account, and the AssumeRoot scoping on
# the admin's permission set. The instance, the AdministratorAccess permission set and the user
# already exist and are read, never managed, here.
data "aws_ssoadmin_instances" "this" {}

locals {
  sso_instance_arn  = one(data.aws_ssoadmin_instances.this.arns)
  identity_store_id = one(data.aws_ssoadmin_instances.this.identity_store_ids)
}

data "aws_ssoadmin_permission_set" "admin" {
  instance_arn = local.sso_instance_arn
  name         = "AdministratorAccess"
}

data "aws_identitystore_user" "admin" {
  identity_store_id = local.identity_store_id

  alternate_identifier {
    unique_attribute {
      attribute_path  = "UserName"
      attribute_value = var.identity_center_user_name
    }
  }
}

# Only the workload account's root, and only the five root tasks. The account ID comes from the
# account resource, never a literal. This set is also provisioned to the management account, and
# the policy applies there too: it only denies sts:AssumeRoot, so no other access changes. A later
# member account needs this policy updated before AssumeRoot can recover it.
resource "aws_ssoadmin_permission_set_inline_policy" "admin_assume_root" {
  instance_arn       = local.sso_instance_arn
  permission_set_arn = data.aws_ssoadmin_permission_set.admin.arn
  inline_policy = templatefile("${path.module}/policies/admin-assume-root.json.tftpl", {
    workload_account_id = aws_organizations_account.workloads.id
  })

  lifecycle {
    # Removing it while root access is on would leave AssumeRoot unscoped.
    prevent_destroy = true
  }
}

# Read-only checks in the workload account. ReadOnlyAccess covers a read-only `terraform plan`
# (-lock=false) of the member stacks: s3:Get*/List* on an SSE-S3 state bucket, iam:Get*/List*.
resource "aws_ssoadmin_permission_set" "readonly" {
  name             = "ecp-readonly"
  description      = "Read-only checks in ecp-workloads (ADR-0021)"
  instance_arn     = local.sso_instance_arn
  session_duration = "PT1H"
}

resource "aws_ssoadmin_managed_policy_attachment" "readonly" {
  instance_arn       = local.sso_instance_arn
  permission_set_arn = aws_ssoadmin_permission_set.readonly.arn
  managed_policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# The admin's sessions in the workload account carry the scoping above from the start.
resource "aws_ssoadmin_account_assignment" "admin_workloads" {
  instance_arn       = local.sso_instance_arn
  permission_set_arn = data.aws_ssoadmin_permission_set.admin.arn
  principal_id       = data.aws_identitystore_user.admin.user_id
  principal_type     = "USER"
  target_id          = aws_organizations_account.workloads.id
  target_type        = "AWS_ACCOUNT"

  depends_on = [aws_ssoadmin_permission_set_inline_policy.admin_assume_root]
}

resource "aws_ssoadmin_account_assignment" "readonly_workloads" {
  instance_arn       = local.sso_instance_arn
  permission_set_arn = aws_ssoadmin_permission_set.readonly.arn
  principal_id       = data.aws_identitystore_user.admin.user_id
  principal_type     = "USER"
  target_id          = aws_organizations_account.workloads.id
  target_type        = "AWS_ACCOUNT"

  depends_on = [aws_ssoadmin_managed_policy_attachment.readonly]
}
