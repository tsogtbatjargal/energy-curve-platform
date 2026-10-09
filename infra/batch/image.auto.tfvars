# What is deployed (ADR-0022). A rollout is a PR that changes image_digest, and a rollback is a
# revert. Nothing private belongs here: the alert address stays in the git-ignored terraform.tfvars.
image_digest     = "sha256:ee716ec0feaed694054c2a90f11f06740b7e38526b35852e18cbe5c737e6e47c"
schedule_enabled = true
