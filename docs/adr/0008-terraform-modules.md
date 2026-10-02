# 8. Build on terraform-aws-modules

Date: 2026-10-02 · Status: accepted

## Decision
- Use the community `terraform-aws-modules` for vpc, security-group, s3-bucket, lambda, ecs, step-functions, rds and elasticache, pinned to exact versions.
- Write raw resources for Glue and Redshift Serverless, which the community redshift module does not cover.
- Layout:
  - `infra/bootstrap`: state bucket, budget, OIDC
  - `infra/batch`
  - `infra/demo`
- Remote state lives in S3 with native lockfile locking.

## Consequences
The modules are widely used and actively maintained, so less custom code needs review. The raw resources still give direct practice with IAM and networking.
