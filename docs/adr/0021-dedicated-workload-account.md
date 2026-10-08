# 21. A dedicated member account for workloads

Date: 2026-10-04 · Status: **proposed** (plan only; no account creation or AWS apply is authorized)

## Context
The user chose (2026-10-04) to run this project's workloads in a dedicated member account, not the organization's management account.

Verified read-only on 2026-10-04:
- **The organization has one account,** the management account, which also hosts Identity Center and billing.
- **No OUs, and SCPs not enabled.** The AWS-managed `FullAWSAccess` policy exists, but the root lists no enabled policy types and nothing is attached. *(Corrected 2026-10-05: this first said "only the default `FullAWSAccess` SCP", as if SCPs were enabled.)*
- **Identity Center** has one permission set, `AdministratorAccess` (12-hour sessions).
- **Other projects in the management account:**
  - another project's GitHub OIDC provider, IAM roles (EKS, GitHub Actions) and service-linked roles, with nothing running behind them (see the R2 pre-apply evidence);
  - the account-wide $20 budget.
- **This project's resources** are all in the management account, in the `infra/bootstrap` stack: the state bucket, the $40 budget, the `project` cost allocation tag activation (`aws_ce_cost_allocation_tag.project`, which the budget's tag filter depends on), `ecp-gha-plan`, `ecp-gha-deploy` and `ecp-workload-boundary` (R2, applied).

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

  The `sts:TaskPolicyArn` condition key restricts which task policies a caller may use. `AssumeRoot` needs a Regional STS endpoint. Its resource is the **target member account's** root, `arn:aws:iam::<member-account-id>:root` (Service Authorization Reference, STS), so a policy that names the target needs that account's ID.
- **Cost allocation tags** can only be activated by the management account of an organization ("Only a management account in an organization and single accounts that aren't members of an organization have access to the cost allocation tags manager"). Activation applies to usage in every member account. The Terraform provider sets a tag to `Inactive` when `aws_ce_cost_allocation_tag` is destroyed.
- **`aws:AssumedRoot`** is present, and true, only in requests made with `AssumeRoot` credentials. AWS's example SCP denies the **long-term** root user (`aws:PrincipalArn` `arn:aws:iam::*:root`) only when `aws:AssumedRoot` is **null**, so it does not block `AssumeRoot` sessions.
- **Price List API:** standard (alert-only) budgets cost $0.00. Only action-enabled budgets are charged, at $0.10 per budget-day after 62 free budget-days a month.

## Proposed decision

### Layout
| Account | Holds |
|---|---|
| **Management** (existing) | Organizations, Identity Center, billing. The other project's resources, untouched. The account-wide $20 budget, untouched. **New:** an `infra/org` stack (OU, the member account, SCPs, the Identity Center assignment, root-access settings). It **actively owns** this project's retained state bucket, $40 budget and `project` cost allocation tag (see *Ownership*). |
| **`ecp-workloads`** (new member, in OU `Workloads`) | Everything this project deploys: a new `infra/bootstrap` instance (state bucket, the GitHub OIDC provider, CI roles, the R2 boundary, and the restricted break-glass role), then `infra/batch` (M4) and `infra/demo` (M6). **No budget and no cost allocation tag.** |

### Ownership of the retained bucket, budget and cost allocation tag
Nothing in the management account is left orphaned, and nothing is duplicated:
- **The state bucket** (`ecp-tfstate-<management>-ca-central-1`, with its versioning, encryption, public-access-block, ownership-controls, lifecycle and policy resources: seven resources) moves from the management `infra/bootstrap` state into `infra/org`:
  - `import` blocks in `infra/org`;
  - `removed { lifecycle { destroy = false } }` blocks in the management `infra/bootstrap`.

  The bucket's `prevent_destroy` stays. It keeps holding `org/terraform.tfstate`, plus the old `bootstrap/terraform.tfstate` key until phase 5 archives it.
  - **Bootstrap still names the bucket.** Its CI roles and the R2 boundary grant state access by the bucket's ARN. They now build it from the bucket name (`local.state_bucket_arn`), which renders the same policy documents, so the bootstrap plan shows no IAM change.
  - **Phase 3 gives the member bootstrap its own bucket at a new address,** created only when a variable says so (true in the member account, false in the management one). The `removed` blocks then stay in place as no-ops for the member instance.
- **Tags are kept as they are.** The imported bucket and budget keep `stack = "bootstrap"`, through a resource-level tag in `infra/org`, so the imports plan no change. Retagging them would be a separate, reviewed change. The OU gets `stack = "org"`.
- **Apply order:** `infra/org` first, then the management bootstrap. In between, both states hold the nine resources, and neither plan changes them.
- **`infra/org` is planned and applied locally by the Identity Center admin, never by CI.** The CI roles still live in the management account until phase 5, and their existing state-read grant covers the whole bucket, so they can read `org/terraform.tfstate`. In phase 1a that state holds the budget's alert address, which CI already has as a secret. Phase 5 removes those roles.
- **The $40 budget** moves the same way: imported into `infra/org`, removed without destroying it from the management bootstrap. It is then **modified in place** to filter on the linked account `ecp-workloads` instead of the `project` tag (phase 1c, Q2). It is never re-created.
- **The `project` cost allocation tag** (`aws_ce_cost_allocation_tag.project`) moves the same way: an `import` block in `infra/org` (ID `project`), and a `removed { lifecycle { destroy = false } }` block in the management bootstrap. A destroy would set the tag `Inactive` and blind the budget's tag filter, so it is never destroyed. Its status stays `Active` throughout. In `infra/org` the budget's `depends_on` points at it, as it does today.
- **The `infra/bootstrap` code loses `budget.tf`:** both the budget and the cost allocation tag. Every instance of the stack, the member one included, therefore creates **neither**:
  - the account keeps exactly two budgets;
  - the tag has one owner, in the only account that can activate it. A member-account instance could not activate it anyway, and a second Terraform owner would fight the first over its status.

### Root access: credentials removed, scoped `AssumeRoot` recovery kept
- **Timing: after the account exists.** The scoping names the target account, and `AssumeRoot`'s resource is that account's root ARN, so it cannot be written before phase 1b returns the ID. All root-access work is therefore in **phase 1c**: nothing about root access, the permission set or `AssumeRoot` is in 1a or 1b. Before 1c, centralized root access is off, so no `AssumeRoot` session can be started against any account.
- **Order within 1c:** first the scoping policy is provisioned on the permission set, then centralized root access is enabled (`depends_on`). There is never a moment when `AssumeRoot` works and is not yet scoped.
- **Credentials removed.** Centralized root access is enabled in `infra/org` (root credentials management and privileged root sessions), and the new account's root credentials are audited in phase 2 and any that exist are deleted (`IAMDeleteRootUserCredentials`). *(Amended 2026-10-06: the audit found none, so nothing was deleted; see phase 2.)*
- **Who may call `sts:AssumeRoot`.** Only the admin's Identity Center role in the management account (`AWSReservedSSO_AdministratorAccess_*`), through an inline policy on the `AdministratorAccess` permission set. It denies `sts:AssumeRoot` unless both:
  - `sts:TaskPolicyArn` is one of the five task policies above;
  - the resource is `arn:aws:iam::<ecp-workloads-id>:root` (a `NotResource` deny), with the ID taken from the `aws_organizations_account` resource, never typed in.

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
   - **1a:** import the state bucket (seven resources), the $40 budget and the `project` cost allocation tag: nine imports. The matching `removed` blocks go in the management bootstrap, which also drops `budget.tf`. **Create** the OU `Workloads`, the phase's only new resource. **No** root-access, permission-set or account-specific change.
   - **1b:** enable SCPs (the organization import above), create the two SCPs and attach them to `Workloads`, then create the account (`depends_on` both attachments) with `parent_id` set to `Workloads`.
     - **Create, then move.** *(Corrected 2026-10-05: this first said the account is created in `Workloads` and "never outside the guardrails".)* `CreateAccount` takes no parent, so AWS creates the account under the organization **root**. The provider (6.67.0, `resourceAccountCreate`) waits for creation to succeed (polling every 10 s, 10-minute timeout), saves the account ID, then calls `MoveAccount` into `Workloads`. Until the move, the account has only `FullAWSAccess`: see *The bootstrap window* below. The plan shows only the final `parent_id`.
     - The account has `close_on_deletion = false`, `role_name = OrganizationAccountAccessRole` and `iam_user_access_to_billing = ALLOW`.
     - It has `prevent_destroy`, and `role_name` is under `ignore_changes`, because Organizations cannot read it back.
     - The email comes from the git-ignored `terraform.tfvars` and is a sensitive variable.
     - The SCPs name no member account ID.
   - **1c** (everything that needs the account ID; the code is `infra/org/identity_center.tf`, `root_access.tf` and `budget.tf`):
     - the Identity Center assignment (`AdministratorAccess`, plus a new `ecp-readonly` set);
     - the budget's linked-account filter;
     - the `AssumeRoot` scoping on the `AdministratorAccess` permission set, then centralized root access, in that order.
     - **Decided (user, 2026-10-06):**
       - **Q1, IAM trusted access:** one approved, out-of-band call before the plan, `aws organizations enable-aws-service-access --service-principal iam.amazonaws.com`, recorded as evidence. The pinned provider's `aws_iam_organizations_features` (6.67.0) requires it ("you must enable trusted access for AWS Identity and Access Management in AWS Organizations"), and so does the IAM User Guide (*Centralize root access for member accounts*). No standalone Terraform resource manages trusted access, and this stack ignores `aws_service_access_principals` so that Identity Center's integration can't be removed. A `precondition` on the features resource stops the plan until the call has been made.
       - **Q2, the budget filter is the workload account only** *(decided 2026-10-06, replacing an OR that could not be applied)*: the existing legacy `cost_filter` changes in place from `TagKeyValue` `user:project$energy-curve-platform` to `LinkedAccount` = `aws_organizations_account.workloads.id`. No `filter_expression`, no `metrics`, no out-of-band `UpdateBudget`.
         - **Why not the OR** (anything in `ecp-workloads`, or anything tagged `project`). It needs `filter_expression`, which provider 6.67.0 supports, with `metrics`. A disposable probe (`plan -refresh=false` on a state holding the legacy filter) showed the switch planning as an in-place `update`, but keeping `cost_filter` and `cost_types`, which are `Optional + Computed`, next to the new fields. The provider then sends all four (`expandBudgetUnmarshal`), and the Budgets API refuses that ("Either FilterExpression and Metrics or CostFilters and CostTypes, not both"). A second `cost_filter` would have been an AND, not an OR.
         - **The tag-key form was never confirmed.** For a `FilterExpression`, the Budgets user guide's `user:` prefix sits with the legacy `TagKeyValue` format (`user:Key$value`). The Cost Explorer `Expression` examples, the same structure, and the provider's acceptance tests use a bare key. No primary source settles it, and with this decision it no longer matters.
         - **Consequence: tagged costs left in the management account are no longer counted** by the $40 budget: today the state bucket (cents a month), and the old `bootstrap/terraform.tfstate` key until phase 5. The account-wide $20 budget still covers the whole management account. The `project` cost allocation tag stays `Active` and owned by `infra/org`.
         - **Probed:** with the new configuration, the same plan-only probe shows `update`, no `replace_paths`, only `cost_filter` changing to `[{name = LinkedAccount, values = [<account>]}]`, `cost_types` kept, `filter_expression` empty and `metrics` null. The request stays the legacy pair (`CostFilters` and `CostTypes`).
       - **Q3, `ecp-readonly` is AWS-managed `ReadOnlyAccess`,** with 1-hour sessions. Its v190 document covers a read-only plan's refresh of the member stacks: `s3:Get*` and `s3:List*` (the state object and the bucket's configuration), `iam:Get*` and `iam:List*`. It has no `kms:Decrypt`, which the SSE-S3 state bucket doesn't need. A plan with it must use `-lock=false`: the S3 lock file is a write. It can read every object, state included.
       - **Q4, the scoping applies wherever `AdministratorAccess` is provisioned,** the management account included. It only denies `sts:AssumeRoot`. A later member account needs the policy updated before `AssumeRoot` can recover it. The account ID comes from `aws_organizations_account.workloads.id`, never a literal; the plan check refuses any 12-digit number in the stack source.
     - **The user name** for the assignments is a new sensitive variable, `identity_center_user_name`, in the git-ignored `terraform.tfvars`.
2. **Access:**
   - the user adds CLI profiles for the account (SSO) and logs in. *(Deviation, 2026-10-06: two profiles, `ecp-workloads-readonly` and `ecp-workloads-admin`, instead of one `ecp-workloads`, so neither can be mistaken for `ecp-admin`; no `[default]` profile was added.)*;
   - the read-only session-start checklist runs;
   - the root credentials are audited through `AssumeRoot` (`IAMAuditRootUserCredentials`), and any that exist are deleted (`IAMDeleteRootUserCredentials`), each session with its own approval.
3. **Member bootstrap** (into `ecp-workloads`). *(Design detailed 2026-10-06, phase 3 code PR.)*
   - **The same stack, a second instance:** `member_instance = true` selects it; the default, `false`, keeps the management instance exactly as it was. The member instance:
     - creates its own state bucket at new addresses (`member_state.tf`), so the `removed` blocks stay no-ops;
     - creates the GitHub OIDC provider, since `ecp-workloads` has none;
     - imports `OrganizationAccountAccessRole` with the restricted trust template (`policies/break-glass-trust.json.tftpl`, `break_glass.tf`). The management account's ID comes from the organization data source, never a literal.

     No budget and no cost allocation tag.
   - **The instance guard** (`guard.tf`): each instance sets `expected_account_id` in its own git-ignored tfvars. The plan stops unless the caller is that account, and unless `member_instance` matches whether it is the organization's management account. Tested offline with a mocked provider (`infra/bootstrap/tests/`, run in CI's `iac-static`). CI derives the value from the plan role it assumes. **The management instance's tfvars needs `expected_account_id` added before its next plan.**
   - **Separate instance files:**
     - the member instance runs with `TF_DATA_DIR=.terraform-member`, `-backend-config=backend-member.hcl` and `-var-file=member.tfvars`;
     - the management instance keeps `.terraform`, `backend.hcl` and `terraform.tfvars`;
     - all of them are git-ignored (`backend*.hcl`, `.terraform-*/`, `*.tfvars`).
   - **First apply with local state:**
     - through a temporary, uncommitted `backend_override.tf` (`backend "local"`). It deliberately isn't git-ignored, so a leftover shows in `git status`.
     - After the apply, it is deleted, then `terraform init -migrate-state` moves the state into the new bucket.
     - *(Found 2026-10-07, [evidence](../evidence/phase3-2026-10-07.md).)* Migrating from a configured local backend leaves the local `terraform.tfstate` unchanged; Terraform empties it only when no backend was configured before. The copy in S3 started a new lineage at serial 1. Its resources, outputs and Terraform version are identical to the local state's, and `check_results` differs only in order. The unused local file is removed in a separate cleanup.
     - Probed offline: `init -backend=false` cannot plan.
   - **The plan check:** `scripts/bootstrap_plan_check.py --change first-apply` accepts exactly the 16 creates and the break-glass import changing only its trust and tags.
     - **It also reads the stack source,** as phases 1b and 1c do. The deploy policy and the CI trust documents are unknown at plan time, so the plan cannot show them, and an override file would merge over them unseen.
     - Over exactly the files Terraform 1.15.8 loads, the one allowed override is `backend_override.tf`, whose content must be exactly the local backend block. Every other override file, any JSON configuration and any module call stops the check, and so does a source file it cannot parse.
     - Terraform rejects the one-line `terraform { backend "local" {} }` (a one-line block may hold only one argument), so the required content is the same block in `terraform fmt` layout, over three lines. The report prints its SHA-256.
     - A run without the override stops.
   - **R2 is re-accepted** (`scripts/r2_accept.py`, 16 cases).
   - **Steps, each a separate approval:**
     1. the code PR;
     2. read-only prechecks:
        - **no GitHub OIDC provider in the member account**;
        - the bucket name is free;
        - the role's current trust;
        - the member admin role's name matches the protect SCP's exemption;
        - the management sweep;
     3. the read-only first-apply plan, with `ecp-workloads-readonly`, the policy gate (`--stack bootstrap`) and the first-apply check (plan and source, including the override's SHA-256), then the hash;
     4. the apply, with `ecp-workloads-admin` (the protect SCP lets only that session create the boundary or change the role), under **the phase 1b pre-apply gates and stop rules**:
        - re-hash;
        - `main` unchanged and a clean tree, apart from the temporary override;
        - no workflow running and no open PR;
        - the sweep admin-only;
        - apply exactly the saved plan;
        - on any failure, stop: no retry, re-plan, untaint, import or recovery without approval;
     5. the state migration;
     6. read-only verification;
     7. the simulations;
     8. **the live break-glass test, required:** the management admin assumes the role with a source identity and makes one read call; the same assumption without a source identity is denied;
     9. the evidence PR.
4. **CI switch** (GitHub settings, done by the user): the repo variables and the `prod` environment point to the member account's roles and bucket, and CI's `terraform-plan` plans the member bootstrap. **CI also needs `member_instance=true`** (`TF_VAR_member_instance`): without it, the instance guard stops the member plan. The `terraform-plan` job takes it from the repo variable `TF_MEMBER_INSTANCE`, defaulting to `false`, so the code can merge first and the user switches `AWS_PLAN_ROLE_ARN`, `TF_STATE_BUCKET`, `TF_MEMBER_INSTANCE=true` and `prod`'s `AWS_DEPLOY_ROLE_ARN` together; a hard-coded value would make every CI plan fail the guard until the settings change. `expected_account_id` already comes from the plan role's ARN.
5. **Decommission** (management account): destroy `ecp-gha-plan`, `ecp-gha-deploy` and `ecp-workload-boundary` from the management bootstrap. The bucket, budget and cost allocation tag are already owned by `infra/org`, so this plan shows **0** changes to them. Then archive `bootstrap/terraform.tfstate`.
   - **The plan:** `terraform plan -destroy` of the management instance, whose whole state is exactly these 8 resources: the two roles, their 3 inline policies and 2 attachments, and the boundary.
   - **The gate:** `scripts/bootstrap_plan_check.py --change decommission` accepts exactly the 8 deletes, each matched by name in its `before` values, as the whole prior state, and the 4 output deletes. It also requires `member_instance = false` in the management account; refuses any other change, import, drift, budget or cost allocation tag; and, from the source, refuses any override file, JSON configuration or module call.
   - **No policy gate on this plan.** `scripts/policy_gate.py` counts only resources that remain, so it exits 2 (`VACUOUS`) on a plan that only deletes. That guard stays as it is for every other plan, and a test pins both behaviours.

After phase 5, M4c (`infra/batch`) is planned and applied **only** in `ecp-workloads`.

### The bootstrap window (phase 1b)
**What is exposed.**
- **W1, from creation to the move**, normally seconds: the account sits under the root with only `FullAWSAccess`, so none of the project SCPs apply. Attaching SCPs to the root to cover it would be an organization-wide change, so it is **not** done without the user's approval.
- **W2, from creation to phase 3:** `OrganizationAccountAccessRole` trusts the whole management account until phase 3 replaces its trust policy, and the protect SCP stops anyone but the Identity Center admin from changing it. W1 lies inside W2.
- **Who can act in the account then.** The root user has no password; it can only be recovered through the account email, which the user controls. Otherwise, any management-account principal allowed `sts:AssumeRole` on the role can, and, once phase 1c turns on centralized root access, any principal allowed `sts:AssumeRoot` on the account's root.
- **The sweep:** `iam:SimulatePrincipalPolicy` for every non-service role **and IAM user** in the management account, for both `sts:AssumeRole` on `arn:aws:iam::<member>:role/OrganizationAccountAccessRole` and `sts:AssumeRoot` on `arn:aws:iam::<member>:root`.
  - **First run** (2026-10-05):
    - the admin's `AWSReservedSSO_AdministratorAccess_*` role: **allowed** both, as intended;
    - **`ecp-gha-deploy`: allowed** both, through PowerUserAccess. This was not intended;
    - `ecp-gha-plan` and the other project's six roles: implicitly denied both.
  - **After PR #25 was applied** (2026-10-05): only the admin role is allowed; `ecp-gha-deploy` is explicitly denied both; the others are unchanged.
  - **Widened to IAM users** (2026-10-06, before the third account apply). *(Correction: the runs above covered roles only.)* The other project's admin IAM user was **allowed** both, through its group's `PowerUserAccess`. The user approved an **out-of-band** inline deny on that group, `ecp-no-cross-account-sts`: the deploy role's `NoCrossAccountRoles` and `NoRootSessions` statements. It is in no Terraform state. Afterwards only the admin role is allowed; that user is explicitly denied both. Its reversal, and the risk that the other project's IaC removes it, are in the [phase 1b evidence](../evidence/phase1b-2026-10-06.md#the-sweep-and-an-out-of-band-iam-change). The sweep is re-run before every later phase.

**Proposed acceptance for the window (each item needs the user's approval):**
1. **Before the apply,** close the unintended path. **Done (2026-10-05):**
   - PR #25 added `NoCrossAccountRoles` (`sts:AssumeRole`, `sts:TagSession` and `sts:SetSourceIdentity` on roles outside the deploy role's own account) and `NoRootSessions` (`sts:AssumeRoot` on every target) to the deploy policy (ADR-0018);
   - it was applied from its approved saved plan, with locking on;
   - the bootstrap re-plan shows no changes, `scripts/r2_accept.py` gives 16 cases and 0 differences, and the sweep shows only the admin role allowed.
2. **Immediately before the apply,** re-run the sweep (both actions), and confirm no workflow is running and nothing is merged to `main` during the apply. Today no workflow uses the `prod` environment or the deploy role.
3. **After the apply:**
   - the account's parent is `Workloads` (`organizations:ListParents`);
   - a re-plan shows no changes, and the account is not tainted;
   - the window is recorded: the `CreateAccount` and `MoveAccount` event times from the management account's CloudTrail (us-east-1);
   - at phase 2, with read access in the account, its CloudTrail shows no `AssumeRole` of `OrganizationAccountAccessRole` and no write events between creation and the move, other than AWS's own setup of that role. **Done (2026-10-06):** none, across every Region ([evidence](../evidence/phase2-2026-10-06.md)).

**Failure and recovery.** Each recovery step is a new plan or state operation, and needs its own approval. No replacement plan is ever applied without one.
- **The apply fails before `CreateAccount`** (enabling SCPs, a policy or an attachment): no account exists; what succeeded is in state. Re-plan, gate, approve.
- **`CreateAccount` is rejected or ends `FAILED`** (for example `EMAIL_ALREADY_EXISTS`): no account exists; the error carries the failure reason. Fix the input, then a new plan and approval.
  - **This happened on 2026-10-06** with the approved plan `8bc85dc5…`. The organization import, SCP enablement, both SCPs and both attachments applied. `CreateAccount` then ended `FAILED` with `EMAIL_ALREADY_EXISTS`: the budget alert address already belongs to an AWS account outside the organization. No account exists, nothing is in state for it, and nothing is tainted ([evidence](../evidence/phase1b-partial-2026-10-06.md)).
  - **A second attempt** (2026-10-06, plan `1dba8da6…`) failed the same way. The new address was the **management account's own email**. No account was created and nothing was tainted. The check now refuses any organization account's email. It cannot see AWS accounts outside the organization, so the user picks an address they control, believe unused, and delivery-test.
  - **Recovery, each step approved separately:**
    1. the user sets an unused, delivery-tested `workload_account_email` in the local `terraform.tfvars`; the budget alert address is unchanged;
    2. a new read-only plan from `main`;
    3. both gates: the policy gate with `--stack org`, and `scripts/phase1b_plan_check.py --mode account-only`;
    4. an apply approval for that exact hash.
  - **`--mode account-only` accepts only this shape:** `0 to import, 1 to add (the account), 0 to change, 0 to destroy`. Specifically:
    - the state is exactly phase 1a plus the organization, the two SCPs and the two attachments;
    - from the plan's prior state, which Terraform refreshed from AWS: SCPs are enabled and nothing else; the feature set is `ALL`; each SCP has its name, its type and exactly its reviewed document; each attachment targets `Workloads` and points at its own policy's ID;
    - every other resource is `no-op`, with no import;
    - the account passes every full-mode rule. Its email is not the rejected alert address, and not any organization account's email (case-insensitively, from `data.aws_organizations_organization`: `accounts`, `non_master_accounts`, `master_account_email`). The check fails closed if those emails are missing or incomplete;
    - the source, override, JSON, module-call and output rules are unchanged;
    - **drift:** the only accepted drift is tags read back as `{}` where state had null, with drift action `update` and the resource planned `no-op`, on the OU and on the two SCPs `aws_organizations_policy.workloads_baseline` and `.workloads_protect`. The SCP cases are from the first account-only plan (2026-10-06, `ec6a9843…`, refused by this check and deleted). Any other tag change, any other attribute, any other address or a planned change still stops. The SCP content and attachment checks read the refreshed state, so this cannot hide a policy change. The full mode still accepts the tags drift on the OU only.
- **The wait times out, or the apply is interrupted, while creation is in progress:** Terraform has no ID, but the account may still finish, under the root. Do not simply re-apply. Find it read-only (`DescribeCreateAccountStatus`, `ListAccounts`). Then a recovery plan with an `import` block for that account ID and `parent_id = Workloads` shows an import plus an in-place move. Gate it and approve it. A second account must never be created.
- **The account is created but `MoveAccount` fails:** the account stays under the root with its ID in state, and Terraform 1.15.8 marks it **tainted** (`maybeTainted`). The next plan proposes a replacement, which `prevent_destroy` refuses, so the account cannot be destroyed or re-created. Recovery:
  1. an approved `terraform untaint aws_organizations_account.workloads` (state only);
  2. a plan that shows an in-place `parent_id` update (the provider's update calls `MoveAccount`), gated and approved, then applied.

  Until the move, treat the account as an incident: do not use it, and check its activity at phase 2.

### SCPs on `Workloads`
These are free, and Organizations enforces them independently of IAM.
- **Enabling them comes first.** SCPs are off for the organization, so phase 1b imports `aws_organizations_organization` into `infra/org` and changes only `enabled_policy_types`, to `SERVICE_CONTROL_POLICY`.
  - **Trusted service access is left alone:** `aws_service_access_principals` is under `ignore_changes`, so this stack can never remove Identity Center's integration. IAM's integration (phase 1c) is one approved, out-of-band call instead. *(Corrected 2026-10-06: this said it gets its own resource; no Terraform resource manages trusted access on its own.)*
  - **The organization has `prevent_destroy`.**
  - **Enabling the type attaches `FullAWSAccess` to the root, every OU and every account.** That changes no permission, and SCPs never restrict the management account.
- **Two SCPs, both attached to `Workloads`:** `ecp-workloads-baseline` (the region, organization and root rules) and `ecp-workloads-protect` (the break-glass lock and the second R2 layer). With `FullAWSAccess`, that is 3 of the 10 SCPs an OU can have (Organizations quotas). Each document is far under the 10,240-character limit. They live in `infra/org/policies/`, and the plan check compares the planned content with them.
- **How they are checked:**
  - before attachment: an offline evaluator (`tests/test_scps.py`, with `tests/iam_eval.py`) checks each rule, and the plan check compares the planned documents with the reviewed files;
  - after the account exists: the policy simulator, which evaluates SCPs, checks them inside the account (acceptance below).
- **Region allow-list:** `ca-central-1`, plus `us-east-1` for global services, as a deny on `aws:RequestedRegion` outside the list. Following AWS's example policy, `cloudfront`, `iam`, `organizations`, `route53` and `support` actions are exempt.
- **No leaving the organization:** deny `organizations:LeaveOrganization`.
- **No long-term root user,** with `AssumeRoot` kept (see above).
- **A break-glass lock** (see above). It denies changes to `OrganizationAccountAccessRole` (trust, policies, boundary, tags, deletion) except by `AWSReservedSSO_AdministratorAccess_*` roles.
- **A second layer for R2:**
  - deny `iam:DeleteRolePermissionsBoundary` for everyone;
  - deny creating, versioning, deleting or tagging `policy/ecp-workload-boundary`, except by the admin's Identity Center session.
- **`FullAWSAccess` stays attached.** SCPs only limit; they never grant.

### Access model
- **People:** the user signs in through Identity Center to `ecp-workloads`, with `AdministratorAccess` for applies and `ecp-readonly` for checks. Management-account access is used only for `infra/org`, billing and `AssumeRoot` recovery.
- **CI:** an OIDC provider and the two roles in `ecp-workloads` (immutable-subject trust, ADR-0010, plus the R2 boundary). GitHub never has credentials for the management account.
- **Break-glass:** the restricted `OrganizationAccountAccessRole` above.

### State
| Stack | Account | Bucket / key | Owner of the bucket |
|---|---|---|---|
| `infra/org` (also the budget and the cost allocation tag) | management | existing bucket, `org/terraform.tfstate` | `infra/org` (imported) |
| `infra/bootstrap` (member) | `ecp-workloads` | new member bucket, `bootstrap/terraform.tfstate` | member `infra/bootstrap` |
| `infra/batch`, `infra/demo` | `ecp-workloads` | new member bucket, one key each | member `infra/bootstrap` |

### Cost
- **Free:** Organizations, OUs, SCPs, Identity Center, root access management and the account itself. Charges roll up to the management account (consolidated billing).
- **Budgets:** the $40 budget is modified in place to filter on the linked account instead of the tag, and stays alert-only, so $0.00. The account keeps two budgets; none is created in `ecp-workloads`. The cost allocation tag stays active, at no charge.
- **New state bucket:** cents per month.
- **The M4 estimate is unchanged:** about $0.45–0.60 a month.
- **Closing the account later** (if ever) has no charge, but AWS keeps a closed account suspended for a post-closure period. `close_on_deletion = false` prevents an accidental closure through Terraform.

## Acceptance criteria
Each phase is accepted only when its checks pass. They are recorded, sanitized, under `docs/evidence/`.

**Phase 1a: ownership transfer and the OU**
- **The `infra/org` plan:** Terraform reports `9 to import, 1 to add, 1 to change, 0 to destroy`.
  - **The 1 to add is the OU** `Workloads`, under the organization root. Imports are not counted as additions.
  - **The 9 imports** are exactly the bucket's seven resources, `aws_budgets_budget.project` and `aws_ce_cost_allocation_tag.project`. Each is a plain import with no attribute change (tags included), with one exception: the budget.
  - **The 1 to change is state-only: the budget import's sensitivity marks.** Found in the first real plan (2026-10-05). The import reads the alert emails from AWS unmarked, while the configuration's sensitive variable marks the whole `notification` set sensitive, so Terraform plans an update with every value identical. Terraform 1.15.8 applies an update that differs only in marks to state alone, without calling the provider (`internal/terraform/node_resource_abstract_instance.go`). The emails stay sensitive. The check accepts only this exact case: that address, action `update`, no value or unknown difference, `notification` newly and fully sensitive, and no other mark changed.
  - **Each import is the very object bootstrap relinquishes:** the same bucket name, budget account and name, and tag key, in the import ID, `before` and `after`. The expected identities come from the bootstrap plan's `forget` changes, read from bootstrap's own state, not from the import itself. Both plans are for the same account, the budget is in it, and the bucket is that account's state bucket.
  - `scripts/org_plan_check.py <org-plan.json> <bootstrap-plan.json>` checks both saved plans together: the shapes and the transfer. The policy gate passes with `--stack org` and `--stack bootstrap`. `terraform state list` then shows the nine resources in `infra/org`.
- **The management bootstrap plan forgets them:** `0 to add, 0 to change, 0 to destroy`, and the same nine resources are listed as no longer managed (`removed` with `destroy = false`, the action `forget`). Nothing else changes, IAM policies and outputs included.
  - **Two reviewed refresh-drift cases are accepted, both on resources planned `no-op`.** Found in the same plan. Drift changes nothing in AWS; the apply only records the refreshed values in state.
    - `aws_iam_policy.workload_boundary`: tags read back as `{}` where state had null.
    - `aws_iam_role.gha_deploy`: of its two inline policies, the stored copy of `ecp-scoped-iam-and-state` predates the R2 apply. Exactly those two names (that one and `terraform-state-read`) must be present, only that one may change, and each refreshed copy must equal its `aws_iam_role_policy` resource in state, planned `no-op`. The refreshed copy of `ecp-scoped-iam-and-state` holds R2's five Deny statements.
  - **Any other drift, all drift in the org plan, and any deferred change stop.** The same `org_plan_check.py` run checks this. `aws_ce_cost_allocation_tag.project` in particular has no destroy or update action, so its status is never set to `Inactive`.
- **The tag is still active:** `ce:ListCostAllocationTags` for key `project` shows `Active` before and after.
- **The budget count is unchanged:** two budgets, the $20 and the $40. The $40's name, amount and alerts are unchanged (`budgets:DescribeBudgets`, sanitized).
- **No root-access or permission-set change:** the plan contains no `aws_ssoadmin_*` or root-access resource, and `iam:ListOrganizationsFeatures` reports no enabled features afterwards.

**Phase 1b: account and SCPs**
- **After the partial apply of 2026-10-06,** the remaining plan creates only the account; `--mode account-only` checks it (see *Failure and recovery*). The post-apply checks below are unchanged.
- **Status (2026-10-06): applied** on the third attempt (plan `7a68242d…`). The post-apply checks and the bootstrap-window times passed ([evidence](../evidence/phase1b-2026-10-06.md)). The SCP simulations inside `ecp-workloads` and the window's in-account activity check wait for phase 2.
- **The plan:** Terraform reports `1 to import, 5 to add, 1 to change, 0 to destroy`. `scripts/phase1b_plan_check.py` must print `OK: exactly the reviewed phase 1b change`, which means:
  - the 1 import (and the 1 change) is the organization, with only `enabled_policy_types` changing, from none to `SERVICE_CONTROL_POLICY`; the feature set and trusted-service principals are unchanged;
  - the 5 additions are the two SCPs (each equal to its reviewed document), their two attachments to `Workloads`, and the account. Each attachment references exactly its own new policy, and its `policy_id` is unknown at plan time. Plan JSON drops functions and literals, so `replace(<policy>.id, ...)` would look the same there. The check therefore also reads the stack source: each attachment is declared once, sets only `policy_id` and `target_id`, and both are the plain references. It reads exactly the files Terraform 1.15.8 loads. An override file (`override.tf`, `override.tf.json`, `*_override.tf`, `*_override.tf.json`) would be merged over the reviewed expression unseen, so it stops the check; so do any JSON configuration and any module call. `infra/org` uses none of them. The check is run from the clean checkout the saved plan was made from. The account is `ecp-workloads`, with `parent_id` `Workloads` (created under the root, then moved), the configured email, still sensitive, `close_on_deletion = false`, the break-glass role name, billing access `ALLOW` and no GovCloud account, created after both attachments;
  - every phase 1a resource is unchanged, and the state holds exactly them;
  - no other change, no deferral, and no drift except the OU's `tags` reading back as `{}` where state had null, with the OU planned `no-op` (the same provider normalization as in phase 1a).
- **The policy gate passes** with `--stack org`.
- **After the apply:** SCPs are enabled on the root; `Workloads` has `FullAWSAccess` plus the two project SCPs; the organization has 2 accounts.
- **The account:** `ecp-workloads` is `ACTIVE`, in OU `Workloads`, with `FullAWSAccess` plus the project SCPs attached.
- **The bootstrap window:** the proposed acceptance above, if the user approves it.
- **Still no root-access change:** the plan contains no `aws_ssoadmin_*` or root-access resource, and `iam:ListOrganizationsFeatures` still reports none enabled.
- **SCP simulations** in `ecp-workloads` (done in phase 2, 2026-10-06, all as expected; [evidence](../evidence/phase2-2026-10-06.md)):
  - a long-term root principal (no `aws:AssumedRoot`) is denied;
  - a request with `aws:AssumedRoot` = `true` under `S3UnlockBucketPolicy` is not denied by the SCP;
  - a request outside the allowed regions is denied;
  - `organizations:LeaveOrganization` is denied.

**Phase 1c: assignment, budget scope, `AssumeRoot` scoping and root access**
- **Status (2026-10-06): applied** (plan `8c9e0a42…`, after a refresh-only apply of the phase 1b drift). Every post-apply check below passed ([evidence](../evidence/phase1c-2026-10-06.md)). One drift entry is accepted: `ecp-readonly`'s tags read back as `{}`, planned `no-op`.
- **Before the plan:** the approved trusted-access call; `organizations:ListAWSServiceAccessForOrganization` then lists exactly `iam.amazonaws.com` and `sso.amazonaws.com`. The sweep of every management-account role and IAM user is admin-only.
- **The plan:** Terraform reports `6 to add, 1 to change, 0 to destroy`, with no import. `scripts/phase1c_plan_check.py` must print `OK: exactly the reviewed phase 1c change`, which means:
  - the 6 additions are the scoping policy (equal to `policies/admin-assume-root.json.tftpl` rendered with the account ID from state, and referencing the account resource), `ecp-readonly` (PT1H, no relay state), its `ReadOnlyAccess` attachment, the two assignments (the configured user, the workload account only, the reference to the account resource), and root access (exactly both features);
  - **the order, read from the plan's configuration:** root access and the admin assignment depend on the scoping policy, and the read-only assignment on its managed policy. The scoping resource provisions the permission set, so the admin role carries it before root access is enabled;
  - the 1 change is the budget, in place, and only its `cost_filter`: exactly one filter, `LinkedAccount` = the workload account's ID from state, referencing `aws_organizations_account.workloads.id`; the `TagKeyValue` filter gone; no `filter_expression` and no `metrics`; nothing else changed (`cost_types` included);
  - phase 1b is intact in the refreshed state (the account in `Workloads`, each SCP its document, each attachment on `Workloads`); the organization's trusted services are exactly `iam` and `sso`; one Identity Center instance; the permission set is `AdministratorAccess`; the user is the configured one;
  - no other change, no output change, and the phase 1b source rules (no override, JSON configuration or module call; no 12-digit literal);
  - **drift: only the refresh of the out-of-band call.** The organization ignores `aws_service_access_principals`, so it is planned `no-op`, but refresh still reports the change in `resource_drift` (disposable probe, 2026-10-06: Terraform 1.15.8, provider 6.67.0, a local Organizations emulator; the human output said "No changes"). The check accepts exactly that entry, once: `aws_organizations_organization.this`, action `update`, only `aws_service_access_principals` differing, `[sso]` → `[iam, sso]`. Any other principals change, any other attribute, action or address, and any other drift (tags included) still stops;
  - the policy gate passes with `--stack org`.
- **The budget:** `budgets:DescribeBudget` shows `CostFilters` `LinkedAccount` = `ecp-workloads` only, no tag filter and no `FilterExpression`; it has no actions; still two budgets. The cost allocation tag is unchanged. A re-plan shows no changes.
- **The assignment:** `AdministratorAccess` and `ecp-readonly` are assigned to the user for `ecp-workloads` only.
- **Root access:** `iam:ListOrganizationsFeatures` shows root credentials management and root sessions enabled.
- **`AssumeRoot` scoping** (`simulate-principal-policy` on the admin's Identity Center role, action `sts:AssumeRoot`, resource `arn:aws:iam::<ecp-workloads-id>:root`, context key `sts:TaskPolicyArn`):
  - allowed with each of the five task policies;
  - denied with any other task policy ARN;
  - denied with any other target account's root ARN (another real account ID in the organization if one exists, otherwise a placeholder ID).
- **Other management-account principals** get the same simulation. Any that is allowed `sts:AssumeRoot` is listed for the user, unchanged.

**Phase 2: access and root credentials**
- **Status (2026-10-06): accepted** ([evidence](../evidence/phase2-2026-10-06.md)), with steps 7 and 8 skipped as recorded deviations.
- `sts:AssumeRoot` with `IAMAuditRootUserCredentials` from the admin's Identity Center role succeeds, and the audit shows **no** root credentials: no login profile, access keys, MFA devices or signing certificates. **Any that exist are deleted** through `IAMDeleteRootUserCredentials`, then audited again, each session with its own approval. *(Amended 2026-10-06: this said the audit shows no credentials "after `IAMDeleteRootUserCredentials`". The first audit found none, so no delete or re-audit session was opened. AWS documents that new Organizations accounts have no root credentials by default, and that with centralized root access, member accounts can't sign in to their root user or recover its password.)*
- The read-only session-start checklist runs clean against `ecp-workloads`.

**Phase 3: member bootstrap and break-glass**
- **Status (2026-10-07): applied, migrated and verified** (plan `606e2a94…`). Steps 2–8 passed, and the management instance's acceptance plan shows no changes and no drift ([evidence](../evidence/phase3-2026-10-07.md)).
- **The management instance, after the phase 3 code change:** a read-only plan shows no changes, and no drift beyond what is already accepted.
- **The plan:** the first-apply check prints `OK`, with the expected resources only. **No `aws_budgets_budget` and no `aws_ce_cost_allocation_tag`** appear anywhere in the plan. A Rego rule and a test fail the member bootstrap on either, and the bootstrap code has no `budget.tf`.
- **The break-glass role's trust policy** (`iam:GetRole`) equals the reviewed template: management account, `ArnLike aws:PrincipalArn` limited to `AWSReservedSSO_AdministratorAccess_*`, `sts:SetSourceIdentity` required, `MaxSessionDuration` of 3600. `scripts/role_trust_review.py` lists no unconditioned cross-account trust.
- **The break-glass lock:** simulating `iam:UpdateAssumeRolePolicy` on the role by the member CI deploy role is denied by the protect SCP (`AllowedByOrganizations` false); the deploy role's own policy also grants IAM only on `ecp-*` names. *(Corrected 2026-10-07: this said "SCP plus boundary". The deploy role has no permissions boundary of its own; the boundary applies to the roles it creates.)*
- **R2 re-acceptance:** `scripts/r2_accept.py` gives 16 cases and 0 differences in `ecp-workloads`. *(Corrected 2026-10-06: this said 13; the script has 16 since PR #25 added the cross-account cases.)* The simulator now also evaluates the project SCPs.
- **The second R2 layer:** simulating `iam:DeleteRolePermissionsBoundary` by an admin-like test identity is denied by the SCP.
- **The live break-glass test (required):** the management admin's Identity Center session can assume `OrganizationAccountAccessRole` with a source identity, and the same assumption without one is denied. *(Done 2026-10-07.)* CloudTrail records the denial only in the management account, and without request parameters, so it is matched by time, caller and error code.

**Phase 4: CI**
- **Status (2026-10-07): done** ([evidence](../evidence/phase4-2026-10-07.md)). After the user's settings switch, CI's `terraform-plan` plans the member instance: `No changes.`, 17 resources, 0 gate failures. No GitHub variable names a management-account role. A re-run of `main`'s job picked up the current repository variables (observed in this run; GitHub's documentation was not relied on).
- CI's `terraform-plan` runs against the member bootstrap with the member plan role and `TF_MEMBER_INSTANCE=true`, with 0 failures through the gate.
- No GitHub variable or secret names a management-account role.

**Phase 5: decommission**
- **Status (2026-10-08): applied and verified** (plan `04440ae1…`, [evidence](../evidence/phase5-2026-10-08.md)). `8 destroyed`; both CI roles and the boundary return `NoSuchEntity`; the management bootstrap state is empty; CI's member plan is still `No changes.`. Precheck 8 passed as a documented deviation: the `infra/org` plan had 4 refresh-drift entries, all no-op (the member's new root email, changed through Account Management beforehand, and the phase 1c tags drift). Still open after the apply: a direct read of the cost allocation tag and of the $20 budget's amount. The old `bootstrap/terraform.tfstate` key stays as an empty state.
- **The management bootstrap plan** destroys exactly `ecp-gha-plan`, `ecp-gha-deploy`, their policies and attachments, and `ecp-workload-boundary`, with **0** changes to the bucket, budget or cost allocation tag. That is checked by `bootstrap_plan_check.py --change decommission`, this plan's gate (the policy gate does not apply to a plan that only deletes).
- **Afterwards:**
  - no `ecp-*` role or policy remains in the management account;
  - the other project's roles, OIDC provider and $20 budget are unchanged (the same `role_trust_review.py` lines, the same budget);
  - two budgets, and the `project` cost allocation tag is still `Active`, owned by `infra/org`.

## Consequences
- **Workloads are contained,** and SCPs become a real guardrail. The management account's IAM surface shrinks back to what the other project and Identity Center need.
- **Recovery keeps working without root passwords.** `AssumeRoot` is limited to named tasks and one admin role, and the long-term root user is blocked.
- **More moving parts:** a second account, profile and state bucket, an `infra/org` stack, and imports across state files.
- **R2 must be re-accepted in the member account.** M4c's Terraform can be written in parallel, but it is planned and applied only after phases 1 to 5.
- **Decided for phase 1 (user, 2026-10-04 and 2026-10-05):**
  - the account email is the budget alert address, kept in the git-ignored `infra/org/terraform.tfvars`. *(Superseded 2026-10-06: that address belongs to an AWS account outside the organization, so the account uses a separate, unused address, in the same file.)*;
  - the account name is `ecp-workloads`;
  - both recommended SCPs (the second R2 layer and the break-glass lock) are included, in `ecp-workloads-protect`.
