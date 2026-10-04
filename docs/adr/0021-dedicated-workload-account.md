# 21. A dedicated member account for workloads

Date: 2026-10-04 · Status: **proposed** (plan only; no account creation or AWS apply is authorized)

## Context
The user chose (2026-10-04) to run this project's workloads in a dedicated member account, not the organization's management account.

Verified read-only on 2026-10-04:
- **The organization has one account,** the management account, which also hosts Identity Center and billing.
- **No OUs, and only the default `FullAWSAccess` SCP.**
- **Identity Center** has one permission set, `AdministratorAccess` (12-hour sessions).
- **Other projects in the management account:**
  - another project's GitHub OIDC provider, IAM roles (EKS, GitHub Actions) and service-linked roles, with nothing running behind them (see the R2 pre-apply evidence);
  - the account-wide $20 budget.
- **This project's resources** are all in the management account: the `infra/bootstrap` stack, made up of the state bucket, the $40 budget, `ecp-gha-plan`, `ecp-gha-deploy` and `ecp-workload-boundary` (R2, applied).

Why move:
- **Containment.** AWS Organizations advises using the management account only for tasks that need it.
- **SCPs work.** SCPs cannot restrict "any action performed by the management account". In a member account they become a second, independent guardrail.
- **A smaller blast radius.** CI deploy credentials and workloads sit outside the account that controls Identity Center, the organization and billing.

Facts this plan relies on (AWS documentation):
- An account created through Organizations gets `OrganizationAccountAccessRole`, an administrator role trusted by the management account.
- Its root user has no credentials by default.
- **Centralized root access** lets the management account remove member root credentials and run root-only tasks through short sessions.
- **Price List API:** standard (alert-only) budgets cost $0.00. Only action-enabled budgets are charged, at $0.10 per budget-day after 62 free budget-days a month.

## Proposed decision

### Layout
| Account | Holds |
|---|---|
| **Management** (existing) | Organizations, Identity Center, billing. The other project's resources, untouched. The account-wide $20 budget, untouched. **New:** an `infra/org` stack (OU, the member account, SCPs, the Identity Center assignment) and its state. The existing state bucket stays for it. |
| **`ecp-workloads`** (new member, in OU `Workloads`) | Everything this project deploys: a new `infra/bootstrap` instance (state bucket, the GitHub OIDC provider, CI roles, the R2 boundary), then `infra/batch` (M4) and `infra/demo` (M6). |

### Phases, each a separate authorization
The pattern in every phase is plan, gate, exact-plan approval, apply, then evidence, as ADR-0018 set out.

1. **`infra/org`** (management account, Terraform):
   - the OU `Workloads`;
   - `aws_organizations_account` `ecp-workloads` with `close_on_deletion = false` and `role_name = OrganizationAccountAccessRole`. The account email is supplied by the user at plan time and never committed;
   - centralized root access, enabled for member accounts, to remove the new account's root credentials;
   - SCPs on `Workloads` (below);
   - the Identity Center assignment of the user to `ecp-workloads`, with the existing `AdministratorAccess` permission set. A narrower `ecp-readonly` set is created for read-only checks.

   This is the only phase that runs in the management account.
2. **Access.** The user adds a CLI profile `ecp-workloads` (SSO) and logs in, interactively. The read-only session-start checklist runs against the new account.
3. **Member bootstrap** (`infra/bootstrap`, applied by the admin into `ecp-workloads`):
   - the same stack, parameterized by account. The GitHub OIDC provider becomes a managed resource there, since no other project's provider exists in that account;
   - first apply with local state, then `terraform init -migrate-state` into its own new state bucket (`ecp-tfstate-<member>-ca-central-1`: versioning, SSE-S3, TLS-only, public access blocked, native lock file);
   - the R2 boundary and deploy policy, from the same templates;
   - `scripts/bootstrap_plan_check.py` gains a mode for a first apply, with the expected resource set;
   - the R2 acceptance re-runs there (`scripts/r2_accept.py`). This time the simulator's SCP evaluation applies.
4. **CI switch** (GitHub settings, done by the user): `AWS_PLAN_ROLE_ARN`, `TF_STATE_BUCKET` and the `prod` environment's `AWS_DEPLOY_ROLE_ARN` point to the member account's roles and bucket. The CI `terraform-plan` matrix plans the member bootstrap.
5. **Decommission this project's management-account workload resources** (exact-plan apply):
   - `ecp-gha-plan`, `ecp-gha-deploy` and `ecp-workload-boundary`;
   - the old `bootstrap/terraform.tfstate` key is archived after a final state check.

   The other project's OIDC provider and roles are never touched, because this stack only ever read the OIDC provider through a data source.

After phase 5, M4c (`infra/batch`) is planned and applied **only** in `ecp-workloads`.

### SCPs on `Workloads`
These are free, and Organizations enforces them independently of IAM.
- **Region allow-list:** `ca-central-1`, plus `us-east-1` for global services (IAM, Organizations, Budgets, Cost Explorer, CloudFront certificates). Implemented as a deny on `aws:RequestedRegion` outside the list, exempting global-service actions.
- **No leaving the organization:** deny `organizations:LeaveOrganization`.
- **No root user:** deny every action where `aws:PrincipalArn` is the account root, which backs up centralized root access.
- **A second layer for R2:**
  - deny `iam:DeleteRolePermissionsBoundary`;
  - deny any write to `policy/ecp-workload-boundary`, except by the admin's Identity Center role.

  This also holds if the deploy role's own policy were ever changed.
- **`FullAWSAccess` stays attached.** SCPs only limit; they never grant.

Each SCP is tested with `simulate-custom-policy` or `simulate-principal-policy`, which evaluate SCPs, before it is attached.

### Access model
- **People:** the user signs in through Identity Center to `ecp-workloads`, with `AdministratorAccess` for applies and `ecp-readonly` for checks. Management-account access is used only for `infra/org` and billing.
- **CI:** an OIDC provider and the two roles in `ecp-workloads`, with the same immutable-subject trust as ADR-0010 and the R2 boundary. GitHub never has credentials for the management account.
- **Break-glass:** `OrganizationAccountAccessRole`, assumable only from the management account's administrators. It is kept and documented, not used day to day.

### State
| Stack | Account | Bucket / key |
|---|---|---|
| `infra/org` | management | existing bucket, `org/terraform.tfstate` |
| `infra/bootstrap` (member) | `ecp-workloads` | new member bucket, `bootstrap/terraform.tfstate` |
| `infra/batch`, `infra/demo` | `ecp-workloads` | new member bucket, one key each |

The CI plan role in the member account reads only the member bucket.

### Cost
- **Free:** Organizations, OUs, SCPs, Identity Center and the account itself. Charges roll up to the management account (consolidated billing).
- **Budgets:** the project's $40 budget changes from a `project`-tag filter to a **linked-account filter** on `ecp-workloads`, plus the tag, in the management account. It stays an alert-only budget, so $0.00, and the budget count stays at two. The account-wide $20 budget is untouched.
- **New state bucket:** cents per month.
- **The M4 estimate is unchanged:** about $0.45–0.60 a month (`.agents/tasks/m4-proposal.md`).
- **Closing the account later** (if ever) has no charge, but AWS keeps a closed account suspended for a post-closure period. `close_on_deletion = false` prevents an accidental closure through Terraform.

## Consequences
- **Workloads are contained,** and SCPs become a real guardrail. The management account's IAM surface shrinks back to what the other project and Identity Center need.
- **More moving parts:** a second account, profile and state bucket, and an `infra/org` stack.
- **R2 must be re-accepted in the member account.** M4c's Terraform can be written in parallel, but it is planned and applied only after phases 1 to 5.
- **To decide at phase 1:**
  - the account email (plus-addressing works);
  - the account name;
  - whether to add the R2 second-layer SCP.
