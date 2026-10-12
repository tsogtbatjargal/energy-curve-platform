# What is deployed (ADR-0022). A rollout is a PR that changes image_digest, and a rollback is a
# revert. Nothing private belongs here: the alert address stays in the git-ignored terraform.tfvars.
image_digest     = "sha256:06860528486f3920494fbf3c9bf4748c4bb2f0a3a1743a2793e06ec3f0e88322"
schedule_enabled = true
