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
    resources = [aws_s3_bucket.tfstate.arn]
  }
  statement {
    sid       = "ReadState"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.tfstate.arn}/*"]
  }
  statement {
    sid       = "ManageLockFiles"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.tfstate.arn}/*.tflock"]
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
# so CI cannot modify roles or policies belonging to anything else in the account.
resource "aws_iam_role_policy_attachment" "gha_deploy_poweruser" {
  role       = aws_iam_role.gha_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

data "aws_iam_policy_document" "gha_deploy_iam" {
  statement {
    sid = "ManageProjectIam"
    actions = [
      "iam:*Role*",
      "iam:*Policy*",
      "iam:*InstanceProfile*",
    ]
    resources = [
      "arn:aws:iam::${local.account_id}:role/ecp-*",
      "arn:aws:iam::${local.account_id}:policy/ecp-*",
      "arn:aws:iam::${local.account_id}:instance-profile/ecp-*",
    ]
  }
  statement {
    sid       = "DenySelfModification"
    effect    = "Deny"
    actions   = ["iam:*"]
    resources = [aws_iam_role.gha_deploy.arn, aws_iam_role.gha_plan.arn]
  }
  statement {
    sid       = "ServiceLinkedRoles"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/aws-service-role/*"]
  }
  statement {
    sid       = "WriteState"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.tfstate.arn}/*"]
  }
}

resource "aws_iam_role_policy" "gha_deploy_iam" {
  name   = "ecp-scoped-iam-and-state"
  role   = aws_iam_role.gha_deploy.id
  policy = data.aws_iam_policy_document.gha_deploy_iam.json
}

resource "aws_iam_role_policy" "gha_deploy_state_read" {
  name   = "terraform-state-read"
  role   = aws_iam_role.gha_deploy.id
  policy = data.aws_iam_policy_document.state_access.json
}
