# 5. Identity and secrets

Date: 2026-10-02 · Status: accepted

## Decision
- **People:** IAM Identity Center user with an admin permission set, used via `aws sso login`. The root user is protected by MFA and not used day to day.
- **CI:** GitHub Actions assumes an AWS role through OIDC. The trust policy is limited to this repo's `main` branch and its `prod` environment. No AWS keys are stored in GitHub.
- **Runtime:** Lambda and ECS task roles with least-privilege policies. Execution roles are separate from task roles.
- **EIA API key:**
  - Cloud: AWS Secrets Manager.
  - Local: `.env`, git-ignored and loaded by mise.
  - CI: a GitHub `prod` environment secret, used only by the opt-in live smoke test.
  - The key is redacted before anything reaches Bronze, logs or hashes, because EIA echoes it in responses.
- **CI hygiene:**
  - `permissions:` set per job with least privilege.
  - Actions pinned to commit SHAs.
  - `shell: bash`.
  - Any credential file created at runtime is deleted in an `if: always()` step.
  - No `pull_request_target`, so forked PRs never receive secrets.
  - Gitleaks runs in pre-commit and in CI.
  - GitHub secret scanning and push protection are enabled.

## Consequences
No long-lived cloud credentials exist anywhere in the repo, CI, or containers.
