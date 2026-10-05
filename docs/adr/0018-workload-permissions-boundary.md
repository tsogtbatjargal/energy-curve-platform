# 18. R2: a permissions boundary on every role the deploy role creates

Date: 2026-10-04 · Status: accepted

## Context
ADR-0010 left a known gap. `ecp-gha-deploy` may manage any `ecp-*` role (`iam:*Role*`, `iam:*Policy*`), so it could create a role, attach any policy, pass the role to a service, and so exceed its own permissions. PLAN.md R2 requires a permissions boundary before any workload stack is applied.

Verified on 2026-10-04 against primary sources:
- **AWS Service Reference JSON** for IAM (`servicereference.us-east-1.amazonaws.com/v1/iam/iam.json`, v1.4):
  - `iam:PermissionsBoundary` is a condition key on `CreateRole`, `PutRolePermissionsBoundary`, `AttachRolePolicy`, `DetachRolePolicy`, `PutRolePolicy`, `DeleteRolePolicy` and `DeleteRolePermissionsBoundary` (and some others);
  - `iam:PolicyARN` is a condition key on `Attach/DetachRolePolicy`.
- **IAM User Guide, condition operators:** with a negated operator (`ArnNotEquals`), a key missing from the request makes the condition true. So a Deny on "boundary not equal to X" also denies a request with no boundary at all.
- **terraform-provider-aws 6.67.0** (`internal/service/iam/role.go`):
  - destroying a role detaches managed policies, deletes inline policies, then calls `DeleteRole`;
  - only an update that clears `permissions_boundary` calls `DeleteRolePermissionsBoundary`.
- **AWS PowerUserAccess v12:** allows everything except `iam:*`, `organizations:*` and `account:*`, plus a few named actions such as `iam:CreateServiceLinkedRole`. That means it also allows `sso:*` and `identitystore:*`.
- **IAM User Guide, permissions boundaries:** a boundary does not limit resource-based policies, and service-linked roles cannot carry one.

## Decision

### The boundary
`infra/bootstrap/boundary.tf` creates the `ecp-workload-boundary` managed policy from `policies/workload-boundary.json.tftpl`. Its ARN is built from the account and name, so the deploy policy and workload stacks know it at plan time. A postcondition checks that it matches the real ARN.

It is an allow-list. ADR-0006 rule 4 forbids `Allow *` on `*`, and the boundary is no exception. It allows:
- the services M4 and M5 use: S3, Lambda, ECS/Fargate with ECR pull, Step Functions, EventBridge and Scheduler, CloudWatch and Logs, SNS, Glue, Redshift Serverless and the Data API, Secrets Manager and SSM parameter reads, and EC2 network-interface management;
- `iam:PassRole` on `role/ecp-*`, and no other IAM action.

It explicitly denies all S3 actions on the Terraform state bucket. Everything else is denied implicitly, including the rest of IAM, Organizations, Account, Identity Center and `sts:AssumeRole`. A new workload service needs a boundary change, applied by the admin.

### The deploy role
`policies/deploy-iam.json.tftpl` replaces the deploy role's inline policy. It keeps ADR-0010's statements and adds Denies. **Denies, not conditional Allows:** the existing `iam:*Role*` Allow already grants these actions, so a second, conditional Allow would restrict nothing.

| Statement | What it denies |
|---|---|
| `RolesOnlyWithTheBoundary` | `CreateRole`, `PutRolePermissionsBoundary`, `AttachRolePolicy`, `DetachRolePolicy`, `PutRolePolicy` and `DeleteRolePolicy`, unless `iam:PermissionsBoundary` is the boundary (`ArnNotEquals`). PLAN.md listed four actions; Detach and DeleteRolePolicy are added so that a role without the boundary cannot be changed at all. |
| `NoBoundaryRemoval` | `DeleteRolePermissionsBoundary`, always. Teardown never needs it. |
| `BoundaryIsFrozen` | Everything except `iam:Get*` and `iam:List*` on the boundary policy's ARN. This covers versions, the default version, deletion, tags, and any future write action. |
| `NoAdministratorAccess` | `AttachRolePolicy` of `AdministratorAccess`, even to a bounded role (PLAN.md R2). |
| `NoIdentityCenter` | `sso:*`, `sso-directory:*` and `identitystore:*`. PowerUserAccess allows them, and the account hosts the Identity Center instance (ADR-0005). |

**Who applies it.** Bootstrap stays applied by the Identity Center admin, never by CI, so the deploy role cannot change its own limits.

### The Rego rule
`terraform.iam` denies every live `aws_iam_role` whose `permissions_boundary` is missing, null, empty, unknown, or not exactly an `…:policy/ecp-workload-boundary` ARN.
- **Which stacks.** It applies in every stack except `bootstrap`. PLAN.md names `batch` and `demo`; a deny-by-default rule also covers stacks added later.
- **How it gets the stack name.** `policy_gate.py` passes `--stack` to conftest as `data.ecp.stack`. A run without it counts as a workload stack, so the rule fails closed.
- **Which roles.** Deleted or forgotten roles are skipped; replacements are checked.

### Tests
- **Rendering.** `tests/r2_policies.py` renders the same template files with synthetic values. It refuses anything Terraform's `templatefile` would interpret differently. A test checks that the `.tf` files pass exactly the templates' placeholders.
- **Offline evaluation.** `tests/iam_eval.py` is a small, strict evaluator. It raises on any policy element it does not support. `tests/test_r2_policies.py` runs the deploy role (PowerUserAccess plus the inline policy) and a bounded `AdministratorAccess` role through the scenarios above, including the Terraform destroy sequence of a bounded role.
- **Mutation check.** Deleting any one statement from either template fails at least one test.
- **Gate integration.** The Rego rule has unit tests, plus a conftest integration test through `policy_gate.main` for `batch`, `demo` and `bootstrap`. CI's python job installs conftest, and `ECP_REQUIRE_CONFTEST` makes that test fail rather than skip.
- **Adversarial tests.** Written black-box from the requirements by a separate helper: `tests/test_r2_adversarial.py` and `policy/terraform/iam_r2_adversarial_test.rego`.

### Evidence from AWS
- **Before apply (read-only):** `aws iam simulate-custom-policy` runs the same scenarios against AWS's own evaluator, with the rendered policies. The boundary is passed as `PermissionsBoundaryPolicyInputList`, and context keys are passed with type `string`. **Done on 2026-10-04:** 41 scenarios, 0 differences from the expected results or the offline evaluator. Details, the role-trust review and the plan comparison are in [the pre-apply evidence](../evidence/r2-preapply-2026-10-04.md).
- **Applying it:** only with the user's explicit approval, and only the exact plan that was checked:
  1. `terraform plan -input=false -out=/tmp/bootstrap-r2.tfplan` in `infra/bootstrap`, with **locking on** (the default).
  2. `terraform show -json /tmp/bootstrap-r2.tfplan > /tmp/bootstrap-r2.json`.
  3. `python scripts/policy_gate.py /tmp/bootstrap-r2.json policy --stack bootstrap --stack-dir infra/bootstrap` must pass.
  4. `python scripts/bootstrap_plan_check.py /tmp/bootstrap-r2.json` must print `OK`. Anything else stops the apply:
     - any change other than creating `aws_iam_policy.workload_boundary` and updating only `policy` on `aws_iam_role_policy.gha_deploy_iam`;
     - a boundary not named exactly `ecp-workload-boundary` at path `/`, or with a name unknown at plan time. Another name or path is another ARN than the one the deploy policy and workload stacks use;
     - any output change other than the new `workload_boundary_arn`;
     - drift, deferred changes, or an errored plan;
     - a planned policy that is unknown, or that differs from its template.
  5. Record `sha256sum /tmp/bootstrap-r2.tfplan` and the check's report; the user approves that exact plan.
  6. `terraform apply -input=false /tmp/bootstrap-r2.tfplan`, with locking on. Terraform refuses a saved plan that has gone stale.
  7. Delete both files. Plan files hold sensitive values in plain text.
- **After an authorized bootstrap apply:** `aws iam simulate-principal-policy` on `ecp-gha-deploy`, per PLAN.md R2:
  - `CreateRole` without the boundary is denied;
  - attaching `AdministratorAccess` is denied;
  - `CreateRole` with the boundary is allowed.

  **Done on 2026-10-04,** after applying the approved saved plan (`1 added, 1 changed, 0 destroyed`): 13 cases, 0 differences, including these three. See [the apply and acceptance evidence](../evidence/r2-postapply-2026-10-04.md); re-run with `scripts/r2_accept.py`.

  This is still a simulation. It evaluates SCPs in scope, but SCPs cannot restrict this account, which is the organization's management account. It does not support resource control policies, and it cannot simulate resource-based policies for an IAM role. The first real proof is the first bounded role the deploy role creates, in M4e. Details are in the evidence.

## Consequences and remaining risks
- **The escalation in ADR-0010 is closed.** Every role the deploy role creates or changes carries the boundary, and the boundary cannot be edited, removed or swapped for another.
- **Resource-based policies are not limited by boundaries.** PowerUserAccess can write bucket, key, queue and Lambda policies that grant a bounded role (or another principal) access outside the boundary. Boundaries don't solve this. Workload stacks are reviewed through the policy gate, and conftest's storage rules cover bucket policies.
- **Service-linked roles cannot carry a boundary.** PowerUserAccess allows `iam:CreateServiceLinkedRole`. Their permissions are fixed by AWS and only the service can assume them, so the risk is accepted.
- **`sts:AssumeRole`.** PowerUserAccess allows it, so any role in the account that trusts the account root would be reachable. Check with a read-only `aws iam list-roles` review in the session-start checklist.
  - **Amended 2026-10-05 (ADR-0021): no roles in other accounts.** A role in another account that trusts this account's root is reachable too, and a new member account's `OrganizationAccountAccessRole` trusts the whole management account by default. A read-only simulation found the deploy role allowed to assume it. The deploy policy now ends that path with `NoCrossAccountRoles`: an explicit Deny on `sts:AssumeRole`, `sts:TagSession` and `sts:SetSourceIdentity` for every role outside the deploy role's own account (`NotResource arn:aws:iam::<account>:role/*`). Roles in its own account are unaffected.
  - **No privileged root sessions:** `NoRootSessions` denies `sts:AssumeRoot` on every target. Its resource is a member account's root, PowerUserAccess allows it, and phase 1c turns on centralized root access (recommended in review).
  - **Tests and evidence:**
    - seven new deploy-role scenarios in `tests/test_r2_policies.py`, also run through AWS's evaluator with `scripts/r2_simulate.py`: 46 cases, 0 differences;
    - three new post-apply cases in `scripts/r2_accept.py`;
    - `scripts/bootstrap_plan_check.py --change no-cross-account` for the saved plan.
  - **Acceptance after the apply** (read-only):
    - `scripts/r2_accept.py` shows every case as expected;
    - `iam:SimulatePrincipalPolicy` for every non-service role in the management account. For `sts:AssumeRole` on `arn:aws:iam::<member>:role/OrganizationAccountAccessRole` and `sts:AssumeRoot` on `arn:aws:iam::<member>:root`, only the Identity Center admin role may be allowed.

    Before this change (2026-10-05): the admin role and `ecp-gha-deploy` were allowed both; `ecp-gha-plan` and the other project's six roles were denied both.
  - **ADR-0021 phase 1b (account creation) waits** until this is applied and verified.
- **Every new workload service needs a boundary change** by the admin. This is deliberate friction.
