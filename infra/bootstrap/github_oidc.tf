# The GitHub OIDC provider is an account-wide singleton, already created by another project.
# Reference it instead of managing it here.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

locals {
  oidc_sub = "token.actions.githubusercontent.com:sub"
  oidc_aud = "token.actions.githubusercontent.com:aud"
}

# --- Plan role: pull requests and main. Read-only AWS plus state lock files. ---

data "aws_iam_policy_document" "gha_plan_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = local.oidc_aud
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = local.oidc_sub
      values = [
        "${var.github_oidc_subject_prefix}:pull_request",
        "${var.github_oidc_subject_prefix}:ref:refs/heads/main",
      ]
    }
  }
}

resource "aws_iam_role" "gha_plan" {
  name                 = "ecp-gha-plan"
  assume_role_policy   = data.aws_iam_policy_document.gha_plan_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "gha_plan_readonly" {
  role       = aws_iam_role.gha_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "state_access" {
  statement {
    sid       = "ListStateBucket"
    actions   = ["s3:ListBucket"]
    resources = [local.state_bucket_arn]
  }
  statement {
    sid       = "ReadState"
    actions   = ["s3:GetObject"]
    resources = ["${local.state_bucket_arn}/*"]
  }
  statement {
    sid       = "ManageLockFiles"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${local.state_bucket_arn}/*.tflock"]
  }
}

resource "aws_iam_role_policy" "gha_plan_state" {
  name   = "terraform-state-read"
  role   = aws_iam_role.gha_plan.id
  policy = data.aws_iam_policy_document.state_access.json
}

# --- Deploy role: only jobs running in the protected "prod" GitHub environment. ---

data "aws_iam_policy_document" "gha_deploy_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = local.oidc_aud
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = local.oidc_sub
      values   = ["${var.github_oidc_subject_prefix}:environment:prod"]
    }
  }
}

resource "aws_iam_role" "gha_deploy" {
  name                 = "ecp-gha-deploy"
  assume_role_policy   = data.aws_iam_policy_document.gha_deploy_trust.json
  max_session_duration = 3600
}

# PowerUserAccess covers service resources but not IAM. IAM is granted only for ecp-* names,
# so CI cannot modify roles or policies belonging to anything else in the account, and every role
# it creates must carry the workload boundary (PLAN.md R2, boundary.tf).
resource "aws_iam_role_policy_attachment" "gha_deploy_poweruser" {
  role       = aws_iam_role.gha_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

# A JSON template, so tests/r2_policies.py renders and evaluates the very same document
# (ADR-0018). jsondecode fails the plan on invalid JSON.
resource "aws_iam_role_policy" "gha_deploy_iam" {
  name = "ecp-scoped-iam-and-state"
  role = aws_iam_role.gha_deploy.id
  policy = jsonencode(jsondecode(templatefile("${path.module}/policies/deploy-iam.json.tftpl", {
    account_id       = local.account_id
    boundary_arn     = local.workload_boundary_arn
    deploy_role_arn  = aws_iam_role.gha_deploy.arn
    plan_role_arn    = aws_iam_role.gha_plan.arn
    state_bucket_arn = local.state_bucket_arn
  })))
}

resource "aws_iam_role_policy" "gha_deploy_state_read" {
  name   = "terraform-state-read"
  role   = aws_iam_role.gha_deploy.id
  policy = data.aws_iam_policy_document.state_access.json
}
