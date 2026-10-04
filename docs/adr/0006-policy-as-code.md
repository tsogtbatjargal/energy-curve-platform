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

Every resource that will exist after apply is checked:
- unchanged resources, so an existing violation still fails
- replacements, whether destroy-first or create-first

Only plan actions that are exactly `["delete"]` or `["forget"]` are excluded. An unknown future action is treated as live, so the gate fails closed. `scripts/policy_gate.py` counts resources with the same rule.

S3 is checked **per bucket**. Each bucket must have its own public access block and encryption configuration. A companion resource covers a bucket when:
- both bucket names are known and equal, or
- its `bucket` argument references that bucket in the same module, with a matching `count`/`for_each` key or a literal instance reference.

For new buckets the companion's `bucket` value is unknown until apply, so the configuration references are what matter. A companion that can be traced neither way is a violation, and the check does not assume it covers anything.

IAM policies whose JSON is unknown at plan time produce a warning from Rego. Since R1 ([ADR-0017](0017-iam-approval-fingerprint-gate.md)), `policy_gate.py --stack <name>` also fails them in every workload stack unless a reviewed approval matches their fingerprint. The bootstrap stack stays exempt.

The policies are unit-tested with `opa test` and checked with `opa check --strict` and `opa fmt --fail`. `scripts/policy_gate.py` runs conftest and exits 0 on pass, 1 on a violation, and 2 when the run proves nothing (no resources in the plan, or an expected policy namespace not loaded), so the gate cannot pass vacuously. `trivy config` runs alongside as an off-the-shelf scanner.

## Consequences
Unsafe infrastructure fails before merge, and each rule has a test showing it catches what it claims to.
