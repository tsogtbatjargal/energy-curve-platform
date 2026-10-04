# R2 apply and acceptance evidence (2026-10-04)

The bootstrap apply that put the workload boundary in place ([ADR-0018](../adr/0018-workload-permissions-boundary.md), PLAN.md R2), and its acceptance check on the deployed deploy role. **Sanitized:** no account ID (`<acct>`), credentials or plan files. The pre-apply evidence is in [r2-preapply-2026-10-04.md](r2-preapply-2026-10-04.md).

## Apply: exactly the approved saved plan
Following ADR-0018's procedure on `main` `39de1a8`:
1. **Plan** with locking on, saved to a file: `Plan: 1 to add, 1 to change, 0 to destroy`.
2. **Policy gate:** 17 resources, 0 failures.
3. **`scripts/bootstrap_plan_check.py`:** `OK: exactly the reviewed R2 change`.
   - The boundary is named `ecp-workload-boundary` at `/`.
   - The deploy policy changes only `policy`.
   - Both documents equal the templates (policy digests `500e035d5d5535d4` and `b904f0426934e455`, as in the pre-apply run).
4. **Approval:** the user approved the saved plan's SHA-256 `6d9f10cb2e1ca6de695250204e4322a97e7d0c170a33cf42858a210f48416e93`. It was re-verified just before applying.
5. **Apply:** `terraform apply` of that file, with locking on:

```text
aws_iam_policy.workload_boundary: Creating...
aws_iam_role_policy.gha_deploy_iam: Modifying... [id=ecp-gha-deploy:ecp-scoped-iam-and-state]
aws_iam_role_policy.gha_deploy_iam: Modifications complete after 0s [id=ecp-gha-deploy:ecp-scoped-iam-and-state]
aws_iam_policy.workload_boundary: Creation complete after 1s [id=arn:aws:iam::<acct>:policy/ecp-workload-boundary]
Apply complete! Resources: 1 added, 1 changed, 0 destroyed.
```

The boundary's ARN postcondition passed. The plan file, its JSON and the log were deleted.

## Acceptance on the deployed role (PLAN.md R2)
`iam:SimulatePrincipalPolicy` on `ecp-gha-deploy`, so every policy really attached to it is evaluated: PowerUserAccess, the new inline policy, and the state-read policy. Re-run with `AWS_PROFILE=ecp-admin uv run python scripts/r2_accept.py`.

```text
OK   iam:CreateRole                     got=explicitDeny expected=explicitDeny PLAN R2: create a role without the boundary
OK   iam:AttachRolePolicy               got=explicitDeny expected=explicitDeny PLAN R2: attach AdministratorAccess (bounded role)
OK   iam:CreateRole                     got=allowed      expected=allowed      PLAN R2: create a role with the boundary
OK   iam:CreateRole                     got=explicitDeny expected=explicitDeny create a role with another boundary
OK   iam:AttachRolePolicy               got=allowed      expected=allowed      attach ReadOnlyAccess to a bounded role
OK   iam:DeleteRolePermissionsBoundary  got=explicitDeny expected=explicitDeny remove a role's boundary
OK   iam:CreatePolicyVersion            got=explicitDeny expected=explicitDeny new version of the boundary policy
OK   iam:DeletePolicy                   got=explicitDeny expected=explicitDeny delete the boundary policy
OK   iam:GetPolicy                      got=allowed      expected=allowed      read the boundary policy
OK   sso:CreatePermissionSet            got=explicitDeny expected=explicitDeny Identity Center
OK   iam:PutRolePolicy                  got=explicitDeny expected=explicitDeny change its own inline policy
OK   iam:DetachRolePolicy               got=allowed      expected=allowed      destroy a bounded role: detach
OK   iam:DeleteRole                     got=allowed      expected=allowed      destroy a bounded role: delete
13 cases, 0 differences
boundary policy: name=ecp-workload-boundary path=/ default=v1 attachments=0 boundary_uses=0
```

**Result: 13 cases, 0 differences.**
- **PLAN.md R2's three:**
  - creating a role without the boundary is denied;
  - attaching AdministratorAccess is denied, even to a bounded role;
  - creating a role with the boundary is allowed.
- **Also denied:** another boundary, removing a boundary, editing or deleting the boundary policy, Identity Center, and changes to the deploy role's own policy.
- **Still allowed:** the destroy steps for a bounded role, so teardown keeps working.

The boundary policy is live at version v1, attached to nothing yet. Workload roles will reference it from M4 on.

**This is a simulation.** `simulate-principal-policy` evaluates the role's identity policies and the context supplied. It does not see AWS Organizations policies (SCPs, RCPs) or resource-based policies. So the first real proof is the first bounded role the deploy role creates, in the M4 first apply (M4e).
