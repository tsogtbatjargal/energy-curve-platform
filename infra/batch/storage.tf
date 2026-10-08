# The data bucket (ADR-0019 store). SSE-S3 rather than a KMS key: synthetic data only (ADR-0020),
# access is IAM-scoped and TLS-only, and a key adds about $1 a month (ADR-0022).
module "data_bucket" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "5.16.2"

  bucket        = local.bucket
  force_destroy = false

  control_object_ownership = true
  object_ownership         = "BucketOwnerEnforced"

  versioning = { enabled = true }

  server_side_encryption_configuration = {
    rule = { apply_server_side_encryption_by_default = { sse_algorithm = "AES256" } }
  }

  attach_deny_insecure_transport_policy = true

  lifecycle_rule = [
    {
      id         = "expire-staging"
      enabled    = true
      filter     = { prefix = "store/staging/" }
      expiration = { days = 7 }
    },
    {
      id                                     = "expire-old-versions"
      enabled                                = true
      filter                                 = {}
      noncurrent_version_expiration          = { noncurrent_days = 30 }
      expiration                             = { expired_object_delete_marker = true }
      abort_incomplete_multipart_upload_days = 1
    },
  ]
}
