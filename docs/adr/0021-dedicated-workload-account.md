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
- **This project's resources** are all in the management account, in the `infra/bootstrap` stack: the state bucket, the $40 budget, `ecp-gha-plan`, `ecp-gha-deploy` and `ecp-workload-boundary` (R2, applied).

Why move:
- **Containment.** AWS Organizations advises using the management account only for tasks that need it.
- **SCPs work.** SCPs cannot restrict "any action performed by the management account". In a member account they become a second, independent guardrail.
- **A smaller blast radius.** CI deploy credentials and workloads sit outside the account that controls Identity Center, the organization and billing.

Facts this plan relies on (AWS documentation, checked 2026-10-04):
- **An account created through Organizations** gets `OrganizationAccountAccessRole`, an administrator role trusted by the management account. Its root user has no credentials by default.
- **Centralized root access** lets the management account remove member root credentials. Root-only recovery then runs through `sts:AssumeRoot`, from a management-account IAM role or user that has that permission explicitly (root credentials cannot call it). Each session is limited to one AWS-managed **task policy**:
  - `IAMAuditRootUserCredentials`
  - `IAMCreateRootUserPassword`
  - `IAMDeleteRootUserCredentials`
  - `S3UnlockBucketPolicy`
  - `SQSUnlockQueuePolicy`

  The `sts:TaskPolicyArn` condition key restricts which task policies a caller may use. `AssumeRoot` needs a Regional STS endpoint.
- **`aws:AssumedRoot`** is present, and true, only in requests made with `AssumeRoot` credentials. AWS's example SCP denies the **long-term** root user (`aws:PrincipalArn` `arn:aws:iam::*:root`) only when `aws:AssumedRoot` is **null**, so it does not block `AssumeRoot` sessions.
- **Price List API:** standard (alert-only) budgets cost $0.00. Only action-enabled budgets are charged, at $0.10 per budget-day after 62 free budget-days a month.

## Proposed decision

### Layout
| Account | Holds |
|---|---|
| **Management** (existing) | Organizations, Identity Center, billing. The other project's resources, untouched. The account-wide $20 budget, untouched. **New:** an `infra/org` stack (OU, the member account, SCPs, the Identity Center assignment, root-access settings). It **actively owns** this project's retained state bucket and $40 budget (see *Ownership*). |
| **`ecp-workloads`** (new member, in OU `Workloads`) | Everything this project deploys: a new `infra/bootstrap` instance (state bucket, the GitHub OIDC provider, CI roles, the R2 boundary, and the restricted break-glass role), then `infra/batch` (M4) and `infra/demo` (M6). **No budget.** |

### Ownership of the retained bucket and budget
Nothing in the management account is left orphaned, and nothing is duplicated:
- **The state bucket** (`ecp-tfstate-<management>-ca-central-1`, with its versioning, encryption, public-access-block and policy resources) moves from the management `infra/bootstrap` state into `infra/org`:
  - `import` blocks in `infra/org`;
  - `removed { lifecycle { destroy = false } }` blocks in the management `infra/bootstrap`.

  The bucket's `prevent_destroy` stays. It keeps holding `org/terraform.tfstate`, plus the old `bootstrap/terraform.tfstate` key until phase 5 archives it.
- **The $40 budget** moves the same way: imported into `infra/org`, removed without destroying it from the management bootstrap. It is then **modified in place** to filter on the linked account `ecp-workloads` as well as the `project` tag. It is never re-created.
- **The `infra/bootstrap` code loses its budget resource.** Every instance of the stack, the member one included, therefore creates **no budget**, so the account keeps exactly two budgets.

### Root access: credentials removed, scoped `AssumeRoot` recovery kept
- **Credentials removed.** Centralized root access is enabled in `infra/org` (root credentials management and privileged root sessions), and the new account's root credentials are deleted (`IAMDeleteRootUserCredentials`).
- **Who may call `sts:AssumeRoot`.** Only the admin's Identity Center role in the management account (`AWSReservedSSO_AdministratorAccess_*`), through an inline policy on the `AdministratorAccess` permission set. It denies `sts:AssumeRoot` unless both:
  - `sts:TaskPolicyArn` is one of the five task policies above;
  - the target is `ecp-workloads`.

  Other management-account principals get no new permission. If one already has admin-level `sts:*` (the other project's roles), the acceptance check below finds it, and it is reported for the user to decide; this plan does not change it.
- **The SCP blocks the long-term root user and keeps recovery.** It uses AWS's pattern: deny `*` when `aws:PrincipalArn` is `arn:aws:iam::*:root` **and** `aws:AssumedRoot` is null. A blanket deny on the root principal would block `AssumeRoot` recovery, so it is not used.

### Break-glass: `OrganizationAccountAccessRole`, restricted
By default this role trusts the whole management account, so any management-account principal with `sts:AssumeRole` could become administrator in `ecp-workloads`. The member bootstrap **imports** the role and replaces its trust policy:
- **Principal:** the management account, **conditioned** on `ArnLike aws:PrincipalArn` = `arn:aws:iam::<management>:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_AdministratorAccess_*`. No other role or user in the management account can assume it.
- **Sessions:** `MaxSessionDuration` of 1 hour, and `sts:SetSourceIdentity` required, so CloudTrail records who assumed it.
- **Permissions:** it keeps `AdministratorAccess`. It is for recovery, not daily use. Day-to-day access goes through Identity Center into `ecp-workloads` directly.
- **SCP lock:** an SCP on `Workloads` denies any change to the role's trust policy or attachments (`iam:UpdateAssumeRolePolicy`, `iam:*RolePolicy`, `iam:DeleteRole`) except by the admin's Identity Center session. The restriction can't be quietly undone.

### Phases, each a separate authorization
The pattern in every phase is plan, gate, exact-plan approval, apply, then evidence, as ADR-0018 set out.
1. **`infra/org`** (management account):
   - **1a:** import the state bucket and the $40 budget, with the matching `removed` blocks in the management bootstrap. The OU `Workloads`. Root-access settings, and the `AssumeRoot` scoping on the `AdministratorAccess` permission set.
   - **1b:** create the account (`close_on_deletion = false`, `role_name = OrganizationAccountAccessRole`, email supplied at plan time and never committed) and attach the SCPs.
   - **1c:** the Identity Center assignment (`AdministratorAccess`, plus a new `ecp-readonly` set) and the budget's linked-account filter. These need the account ID, so they run after 1b.
2. **Access:**
   - the user adds a CLI profile `ecp-workloads` (SSO) and logs in;
   - the read-only session-start checklist runs;
   - the root credentials are deleted through `AssumeRoot` (`IAMDeleteRootUserCredentials`).
3. **Member bootstrap** (into `ecp-workloads`):
   - the same stack, parameterized by account, with no budget;
   - the GitHub OIDC provider becomes a managed resource;
   - `OrganizationAccountAccessRole` is imported with the restricted trust policy;
   - first apply with local state, then `terraform init -migrate-state` into its new bucket;
   - `scripts/bootstrap_plan_check.py` gains a first-apply mode with the expected resource set;
   - R2 is re-accepted (`scripts/r2_accept.py`).
4. **CI switch** (GitHub settings, done by the user): the repo variables and the `prod` environment point to the member account's roles and bucket, and CI's `terraform-plan` plans the member bootstrap.
5. **Decommission** (management account): destroy `ecp-gha-plan`, `ecp-gha-deploy` and `ecp-workload-boundary` from the management bootstrap. The bucket and budget are already owned by `infra/org`, so this plan shows **0** changes to them. Then archive `bootstrap/terraform.tfstate`.

After phase 5, M4c (`infra/batch`) is planned and applied **only** in `ecp-workloads`.

### SCPs on `Workloads`
These are free, and Organizations enforces them independently of IAM. Each is evaluated with the policy simulator, which evaluates SCPs, before it is attached.
- **Region allow-list:** `ca-central-1`, plus `us-east-1` for global services, as a deny on `aws:RequestedRegion` outside the list. Global-service actions are exempt.
- **No leaving the organization:** deny `organizations:LeaveOrganization`.
- **No long-term root user,** with `AssumeRoot` kept (see above).
- **A break-glass lock** (see above).
- **A second layer for R2:**
  - deny `iam:DeleteRolePermissionsBoundary`;
  - deny writes to `policy/ecp-workload-boundary`, except by the admin's Identity Center session.
- **`FullAWSAccess` stays attached.** SCPs only limit; they never grant.

### Access model
- **People:** the user signs in through Identity Center to `ecp-workloads`, with `AdministratorAccess` for applies and `ecp-readonly` for checks. Management-account access is used only for `infra/org`, billing and `AssumeRoot` recovery.
- **CI:** an OIDC provider and the two roles in `ecp-workloads` (immutable-subject trust, ADR-0010, plus the R2 boundary). GitHub never has credentials for the management account.
- **Break-glass:** the restricted `OrganizationAccountAccessRole` above.

### State
| Stack | Account | Bucket / key | Owner of the bucket |
|---|---|---|---|
| `infra/org` | management | existing bucket, `org/terraform.tfstate` | `infra/org` (imported) |
| `infra/bootstrap` (member) | `ecp-workloads` | new member bucket, `bootstrap/terraform.tfstate` | member `infra/bootstrap` |
| `infra/batch`, `infra/demo` | `ecp-workloads` | new member bucket, one key each | member `infra/bootstrap` |

### Cost
- **Free:** Organizations, OUs, SCPs, Identity Center, root access management and the account itself. Charges roll up to the management account (consolidated billing).
- **Budgets:** the $40 budget is modified in place to filter on the linked account as well as the tag, and stays alert-only, so $0.00. The account keeps two budgets; none is created in `ecp-workloads`.
- **New state bucket:** cents per month.
- **The M4 estimate is unchanged:** about $0.45–0.60 a month.
- **Closing the account later** (if ever) has no charge, but AWS keeps a closed account suspended for a post-closure period. `close_on_deletion = false` prevents an accidental closure through Terraform.

## Acceptance criteria
Each phase is accepted only when its checks pass. They are recorded, sanitized, under `docs/evidence/`.

**Phase 1a: ownership and root-access scoping**
- **The `infra/org` plan imports and changes nothing:** `N to import, 0 to add, 0 to change, 0 to destroy` for the bucket and its configuration resources and the budget. `terraform state list` shows them in `infra/org`.
- **The management bootstrap plan forgets them:** they appear as `removed` with `destroy = false`, and the plan shows `0 to destroy`.
- **The budget count is unchanged:** two budgets, the $20 and the $40. The $40's name, amount and alerts are unchanged (`budgets:DescribeBudgets`, sanitized).
- **`AssumeRoot` scoping** (`simulate-principal-policy` on the admin's Identity Center role, with action `sts:AssumeRoot`):
  - allowed with each of the five task policies against `ecp-workloads`;
  - denied with any other task policy or any other target account.
- **Other management-account principals** get the same simulation. Any that is allowed `sts:AssumeRoot` is listed for the user, unchanged.

**Phase 1b–1c: account, SCPs, assignment, budget scope**
- **The account:** `ecp-workloads` is `ACTIVE`, in OU `Workloads`, with `FullAWSAccess` plus the project SCPs attached.
- **The budget:** its filter includes the linked account `ecp-workloads` and the `project` tag; it has no actions; still two budgets.
- **SCP simulations** in `ecp-workloads`:
  - a long-term root principal (no `aws:AssumedRoot`) is denied;
  - a request with `aws:AssumedRoot` = `true` under `S3UnlockBucketPolicy` is not denied by the SCP;
  - a request outside the allowed regions is denied;
  - `organizations:LeaveOrganization` is denied.

**Phase 2: access and root credentials**
- `sts:AssumeRoot` with `IAMAuditRootUserCredentials` from the admin's Identity Center role succeeds. `iam:GetLoginProfile`, `ListAccessKeys` and `ListMFADevices` for the root user then show **no** credentials after `IAMDeleteRootUserCredentials`.
- The read-only session-start checklist runs clean against `ecp-workloads`.

**Phase 3: member bootstrap and break-glass**
- **The plan:** the first-apply check prints `OK`, with the expected resources only. **No `aws_budgets_budget`** appears anywhere in the plan (also a Rego/test check).
- **The break-glass role's trust policy** (`iam:GetRole`) equals the reviewed template: management account, `ArnLike aws:PrincipalArn` limited to `AWSReservedSSO_AdministratorAccess_*`, `sts:SetSourceIdentity` required, `MaxSessionDuration` of 3600. `scripts/role_trust_review.py` lists no unconditioned cross-account trust.
- **The break-glass lock:** simulating `iam:UpdateAssumeRolePolicy` on the role by the member CI deploy role is denied (SCP plus boundary).
- **R2 re-acceptance:** `scripts/r2_accept.py` gives 13 cases and 0 differences in `ecp-workloads`. The simulator now also evaluates the project SCPs.
- **The second R2 layer:** simulating `iam:DeleteRolePermissionsBoundary` by an admin-like test identity is denied by the SCP.

**Phase 4: CI**
- CI's `terraform-plan` runs against the member bootstrap with the member plan role, with 0 failures through the gate.
- No GitHub variable or secret names a management-account role.

**Phase 5: decommission**
- **The management bootstrap plan** destroys exactly `ecp-gha-plan`, `ecp-gha-deploy`, their policies and attachments, and `ecp-workload-boundary`, with **0** changes to the bucket or budget. That is checked by an expected-change check like `bootstrap_plan_check.py`.
- **Afterwards:**
  - no `ecp-*` role or policy remains in the management account;
  - the other project's roles, OIDC provider and $20 budget are unchanged (the same `role_trust_review.py` lines, the same budget);
  - two budgets.

## Consequences
- **Workloads are contained,** and SCPs become a real guardrail. The management account's IAM surface shrinks back to what the other project and Identity Center need.
- **Recovery keeps working without root passwords.** `AssumeRoot` is limited to named tasks and one admin role, and the long-term root user is blocked.
- **More moving parts:** a second account, profile and state bucket, an `infra/org` stack, and imports across state files.
- **R2 must be re-accepted in the member account.** M4c's Terraform can be written in parallel, but it is planned and applied only after phases 1 to 5.
- **To decide at phase 1:**
  - the account email (plus-addressing works);
  - the account name;
  - whether the second R2 layer and the break-glass lock SCPs are added. Both are recommended.
