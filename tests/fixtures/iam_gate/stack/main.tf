# Fixture stack for the R1 IAM approval gate (ADR-0017). It is planned offline with dummy
# credentials (no AWS calls: aws_iam_policy_document is evaluated locally) and never applied.
terraform {
  required_version = "~> 1.15"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.67"
    }
  }
}

provider "aws" {
  region                      = "ca-central-1"
  access_key                  = "fixture"
  secret_key                  = "fixture"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
}

variable "suffix" {
  type    = string
  default = "fixture"
}

locals {
  read_actions = ["s3:GetObject", "s3:ListBucket"]
}

module "label" {
  source    = "cloudposse/label/null"
  version   = "0.25.0"
  namespace = "ecp"
  name      = var.suffix
}

module "logs" {
  source = "./modules/logs"
  prefix = module.label.id
}

resource "aws_s3_bucket" "data" {
  bucket = "ecp-${var.suffix}-data"
}

# A policy from a data source: the plan JSON shows only a reference to its .json.
data "aws_iam_policy_document" "read" {
  statement {
    actions   = local.read_actions
    resources = [aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/*"]
  }
}

resource "aws_iam_role" "task" {
  name = "ecp-${var.suffix}-task"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "read" {
  name   = "read"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.read.json
}

# A policy from jsonencode: the plan JSON drops the literal actions, keeping only references.
resource "aws_iam_policy" "write" {
  name = "ecp-${var.suffix}-write"
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.data.arn}/*" }]
  })
}

# A policy reaching into a local module, which itself uses a registry module's output.
data "aws_iam_policy_document" "logs" {
  statement {
    actions   = ["logs:PutLogEvents"]
    resources = [module.logs.group_arn]
  }
}

resource "aws_iam_policy" "logs" {
  name   = "ecp-${var.suffix}-logs"
  policy = data.aws_iam_policy_document.logs.json
}
