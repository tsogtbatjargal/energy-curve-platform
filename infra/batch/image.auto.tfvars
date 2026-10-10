# What is deployed (ADR-0022). A rollout is a PR that changes image_digest, and a rollback is a
# revert. Nothing private belongs here: the alert address stays in the git-ignored terraform.tfvars.
image_digest     = "sha256:786f802cb415b3a98702554a227926d18142dd7b28288805fffc49d460ad2150"
schedule_enabled = true
