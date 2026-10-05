# The Terraform state bucket, created by infra/bootstrap and owned by this stack since ADR-0021
# phase 1a: imported here, and removed without destroying it from the management bootstrap
# (infra/bootstrap/moved_to_org.tf). The configuration is unchanged, so the imports plan no change.
data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = "ecp-tfstate-${local.account_id}-${var.region}"
  # The bucket and the budget keep the tags bootstrap gave them, so the imports change nothing.
  # Retagging them to stack = "org" would be a separate, reviewed change.
  imported_tags = { stack = "bootstrap" }
}

import {
  to = aws_s3_bucket.tfstate
  id = local.state_bucket
}

import {
  to = aws_s3_bucket_versioning.tfstate
  id = local.state_bucket
}

import {
  to = aws_s3_bucket_server_side_encryption_configuration.tfstate
  id = local.state_bucket
}

import {
  to = aws_s3_bucket_public_access_block.tfstate
  id = local.state_bucket
}

import {
  to = aws_s3_bucket_ownership_controls.tfstate
  id = local.state_bucket
}

import {
  to = aws_s3_bucket_lifecycle_configuration.tfstate
  id = local.state_bucket
}

import {
  to = aws_s3_bucket_policy.tfstate
  id = local.state_bucket
}

resource "aws_s3_bucket" "tfstate" {
  bucket = local.state_bucket
  tags   = local.imported_tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3 rather than a KMS CMK: state holds no secrets by design, access is IAM-scoped and TLS-only,
# and a CMK adds ~$1/month plus request costs for no material gain here.
#trivy:ignore:AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Old state versions are kept 90 days for recovery, then expire.
resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    id     = "expire-noncurrent-state"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "tfstate_tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.tfstate.arn,
      "${aws_s3_bucket.tfstate.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  policy = data.aws_iam_policy_document.tfstate_tls_only.json

  depends_on = [aws_s3_bucket_public_access_block.tfstate]
}
