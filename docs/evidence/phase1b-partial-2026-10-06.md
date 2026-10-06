# ADR-0021 phase 1b: partial apply (2026-10-06)

The approved apply of the phase 1b saved plan stopped when AWS refused to create the account. **Sanitized:** no account IDs, organization, OU or policy IDs, email or plan file. No retry, replacement plan, untaint or recovery mutation was run.

## Before the apply
- **Approval:** the user approved only the saved plan with SHA-256 `8bc85dc542ea70b531ff3701ad94c846a0d4a3d516003037d1150125cd0635a5`. It was made read-only (`-lock=false`) from `main` `030426a`. Terraform reported `1 to import, 5 to add, 1 to change, 0 to destroy`; the policy gate (`--stack org`) and `phase1b_plan_check.py` both passed.
- **Prerequisites, all met:**
  - the AssumeRole/AssumeRoot sweep allowed only the Identity Center admin role. `ecp-gha-deploy` was explicitly denied; `ecp-gha-plan` and the other project's six roles were implicitly denied. *(Corrected 2026-10-06: this sweep covered roles only. A later sweep that included IAM users found the other project's user `bedoux-admin` allowed both actions; see the [phase 1b evidence](phase1b-2026-10-06.md#the-sweep-and-an-out-of-band-iam-change).)*;
  - no workflow run was in progress;
  - `main` was `030426a`;
  - the hash matched.

## The apply (2026-10-06 00:15 UTC, locking on)
```text
aws_organizations_organization.this: Import complete
aws_organizations_organization.this: Modifications complete after 3s
aws_organizations_policy.workloads_baseline: Creation complete after 0s
aws_organizations_policy.workloads_protect: Creation complete after 0s
aws_organizations_policy_attachment.workloads_baseline: Creation complete after 1s
aws_organizations_policy_attachment.workloads_protect: Creation complete after 1s
aws_organizations_account.workloads: Creating...
Error: waiting for AWS Organizations Account (ecp-workloads) create: unexpected state 'FAILED',
wanted target 'SUCCEEDED'. last error: EMAIL_ALREADY_EXISTS
```

## State afterwards (read-only)
| Check | Result |
|---|---|
| Accounts in the organization | 1 (the management account); no member account was created |
| Create-account requests | one `FAILED` request for `ecp-workloads`, `EMAIL_ALREADY_EXISTS` |
| `infra/org` state | phase 1a's 10 resources, the organization, both SCPs and both attachments; **no account, nothing tainted** |
| SCP policy type on the root | `SERVICE_CONTROL_POLICY` `ENABLED` |
| SCPs on the root | `FullAWSAccess` only |
| SCPs on `Workloads` | `FullAWSAccess`, `ecp-workloads-baseline`, `ecp-workloads-protect` |
| Accounts in `Workloads` | 0 |
| Trusted service access | `sso.amazonaws.com`, unchanged |

## Cause and recovery
- **The budget alert address is already the email of an AWS account outside the organization.** The pre-flight check could only see accounts inside the organization.
- **Recovery** is the account-only path in ADR-0021's *Failure and recovery*: a new, unused account email, then a new plan checked with `phase1b_plan_check.py --mode account-only`, then a new apply approval. The spent plan file and its logs were deleted.
