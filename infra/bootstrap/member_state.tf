# ADR-0021 phase 3: the member instance's own state bucket, the same configuration infra/org keeps
# for the management bucket. New addresses, so the removed blocks in moved_to_org.tf stay no-ops.
# The first apply uses local state; the state then moves here with `terraform init -migrate-state`.
locals {
  member_state = var.member_instance ? 1 : 0
}

resource "aws_s3_bucket" "member_state" {
  count  = local.member_state
  bucket = local.state_bucket

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "member_state" {
  count  = local.member_state
  bucket = aws_s3_bucket.member_state[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3 rather than a KMS CMK: state holds no secrets by design, access is IAM-scoped and TLS-only,
# and a CMK adds ~$1/month plus request costs for no material gain here.
#trivy:ignore:AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "member_state" {
  count  = local.member_state
  bucket = aws_s3_bucket.member_state[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "member_state" {
  count                   = local.member_state
  bucket                  = aws_s3_bucket.member_state[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "member_state" {
  count  = local.member_state
  bucket = aws_s3_bucket.member_state[0].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Old state versions are kept 90 days for recovery, then expire.
resource "aws_s3_bucket_lifecycle_configuration" "member_state" {
  count  = local.member_state
  bucket = aws_s3_bucket.member_state[0].id

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

data "aws_iam_policy_document" "member_state_tls_only" {
  count = local.member_state

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.member_state[0].arn,
      "${aws_s3_bucket.member_state[0].arn}/*",
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

resource "aws_s3_bucket_policy" "member_state" {
  count  = local.member_state
  bucket = aws_s3_bucket.member_state[0].id
  policy = data.aws_iam_policy_document.member_state_tls_only[0].json

  depends_on = [aws_s3_bucket_public_access_block.member_state]
}
