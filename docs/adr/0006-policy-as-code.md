# 6. Policy as code on Terraform plans

Date: 2026-10-02 · Status: accepted

## Context
Most of this infrastructure code is AI-assisted. Review alone does not scale, so guardrails must be executable.

## Decision
CI runs `terraform plan`, converts it with `terraform show -json`, and evaluates it with **conftest** against Rego policies in `policy/`:

1. No security group ingress from `0.0.0.0/0` or `::/0` to admin or database ports (22, 3389, 5432, 6379, 5439).
2. S3 buckets block public access and use encryption.
3. Required tags: `project`, `owner`, `cost-center`, `ttl`.
4. No IAM statement allows `*` on `*`, and no `AdministratorAccess` attachment.
5. No `aws_nat_gateway` outside the demo stack (checked through the `stack` tag).
6. RDS instances and Redshift Serverless workgroups are not publicly accessible. ElastiCache has no public-access setting: it is VPC-only, and rule 1 covers its security groups.

Every resource that will exist after apply is checked, including unchanged ones, so an existing violation still fails. S3 buckets are matched to their access-block and encryption resources by count, because bucket IDs are often unknown at plan time. IAM policies whose JSON is unknown at plan time produce a warning and are checked on the next plan. The policies are unit-tested with `opa test` and checked with `opa check --strict` and `opa fmt --fail`. `scripts/policy_gate.py` runs conftest and exits 0 on pass, 1 on a violation, and 2 when the run proves nothing (no resources in the plan, or an expected policy namespace not loaded), so the gate cannot pass vacuously. `trivy config` runs alongside as an off-the-shelf scanner.

## Consequences
Unsafe infrastructure fails before merge, and each rule has a test showing it catches what it claims to.
