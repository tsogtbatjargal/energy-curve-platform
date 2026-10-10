# What Glue runs. The plan check recomputes both hashes from the clean checkout and compares them
# with these objects, so the job is exactly the reviewed source (ADR-0023). `etag` is the MD5 of a
# single-part upload, so a changed file is a visible update in the plan.
resource "aws_s3_object" "script" {
  bucket = module.bucket.s3_bucket_id
  key    = local.script_key
  source = local.script_path
  etag   = filemd5(local.script_path)
}

resource "aws_s3_object" "bundle" {
  bucket = module.bucket.s3_bucket_id
  key    = local.bundle_key
  source = local.bundle_path
  etag   = filemd5(local.bundle_path)
}
