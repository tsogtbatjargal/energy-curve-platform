# The batch image (ADR-0020). Pushed by digest; tags can never move.
#trivy:ignore:AWS-0033 AES256 (SSE-S3-equivalent) rather than a KMS key: the image holds no secret.
resource "aws_ecr_repository" "batch" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "batch" {
  repository = aws_ecr_repository.batch.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 5 newest images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# Same account: one side must allow Lambda to pull, and the stage role has no ECR access. Without
# this, CreateFunction would make Lambda write its own statement here, out of band (Lambda
# Developer Guide, "Amazon ECR permissions"). Stage 1, so stage 2 changes no policy. The statement
# is the documented one (the guide's cross-account example): `aws:sourceARN` with a function
# wildcard for this account and region. Nothing documents an exact function ARN or an
# `aws:SourceAccount` condition for Lambda's pull, so neither is used.
resource "aws_ecr_repository_policy" "batch" {
  repository = aws_ecr_repository.batch.name
  policy     = jsonencode(jsondecode(templatefile("${path.module}/policies/ecr-repository.json.tftpl", local.policy_values)))
}
