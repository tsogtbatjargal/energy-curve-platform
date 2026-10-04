# 10. CI roles and IAM scope

Date: 2026-10-02 · Status: accepted

## Context
GitHub Actions needs AWS access for two jobs: `terraform plan` on pull requests and `apply` on `main`. The account already has a GitHub OIDC provider created by another project. An account can have only one provider per issuer URL.

## Decision
- Reference the existing OIDC provider through a data source; do not manage it here.
- Trust policies match GitHub's **immutable** subject format, `repo:<owner>@<owner_id>/<repo>@<repo_id>:...`, which this repo has enabled. A renamed repo, or a recreated repo with the same name, cannot match it. The first CI run with the classic `repo:<owner>/<repo>` format was denied.
- `ecp-gha-plan`: trusted for `<subject>:pull_request` and `<subject>:ref:refs/heads/main`. Permissions: `ReadOnlyAccess`, state read, and lock-file write. GitHub does not issue OIDC tokens to pull requests from forks, so outside contributors cannot assume it.
- `ecp-gha-deploy`: trusted only for `<subject>:environment:prod`. The `prod` environment needs a reviewer's approval and allows only the `main` branch. Permissions:
  - `PowerUserAccess`
  - IAM actions limited to `ecp-*` roles, policies and instance profiles
  - an explicit deny on modifying either CI role
  - state write
- The state bucket uses SSE-S3 rather than a KMS key; the trivy finding is suppressed inline with the reason.

## Known gap
The deploy role can create an `ecp-*` role, attach any managed policy to it, and pass it to a service, so in principle it could escalate its own privileges. The fix is a **permissions boundary**:

- Create a managed policy `ecp-workload-boundary`.
- Allow `iam:CreateRole`, `iam:PutRolePermissionsBoundary`, `iam:AttachRolePolicy` and `iam:PutRolePolicy` only when the `iam:PermissionsBoundary` condition equals that policy.
- Deny changes to the boundary policy itself.

This must be in place before any workload stack is deployed: requirement R2 in [PLAN.md](../PLAN.md#requirements-before-workload-deployment).

**Update (2026-10-04):** closed by [ADR-0018](0018-workload-permissions-boundary.md). It uses explicit Denies rather than conditional Allows, and adds `DetachRolePolicy`, `DeleteRolePolicy`, `DeleteRolePermissionsBoundary` and an Identity Center deny.
