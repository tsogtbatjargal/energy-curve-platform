# 6. Policy as code on Terraform plans

Date: 2026-10-02 · Status: accepted

## Context
Most of this infrastructure code is AI-assisted. Review alone does not scale, so guardrails must be executable.

## Decision
CI runs `terraform plan`, converts it with `terraform show -json`, and evaluates it with **conftest** against Rego policies in `policy/`:

1. No security group ingress from `0.0.0.0/0` or `::/0` to admin or database ports (22, 3389, 5432, 6379, 5439).
2. S3 buckets block public access and use encryption.
3. Required tags: `project`, `owner`, `cost-center`, `ttl`.
4. No IAM statement allows `*` on `*`.
5. No `aws_nat_gateway` in the batch profile.
6. RDS and ElastiCache are not publicly accessible.

The policies are unit-tested with `opa test` and checked with `opa check --strict` and `opa fmt --fail`. The gate also fails if no policy actually evaluated anything, so it cannot pass vacuously. `trivy config` runs alongside as an off-the-shelf scanner.

## Consequences
Unsafe infrastructure fails before merge, and each rule has a test showing it catches what it claims to.
