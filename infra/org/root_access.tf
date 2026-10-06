# ADR-0021 phase 1c: centralized root access, only after the AssumeRoot scoping is provisioned
# (depends_on). The provider requires IAM trusted access in Organizations first. This stack never
# manages trusted access (aws_service_access_principals is ignored, so Identity Center's
# integration can't be removed here), so that is one approved, out-of-band call before the plan:
#   aws organizations enable-aws-service-access --service-principal iam.amazonaws.com
# The precondition stops the plan until it has been made.
resource "aws_iam_organizations_features" "root_access" {
  enabled_features = ["RootCredentialsManagement", "RootSessions"]

  depends_on = [aws_ssoadmin_permission_set_inline_policy.admin_assume_root]

  lifecycle {
    precondition {
      condition     = contains(data.aws_organizations_organization.this.aws_service_access_principals, "iam.amazonaws.com")
      error_message = "IAM trusted access is not enabled in Organizations: the approved out-of-band step comes first (ADR-0021 phase 1c)."
    }
  }
}
