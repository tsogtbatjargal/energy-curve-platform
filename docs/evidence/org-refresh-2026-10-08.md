# `infra/org` refresh-only apply, budget amounts and cost allocation tag (2026-10-08)

This closes the items that the [phase 5 evidence](phase5-2026-10-08.md) of [ADR-0021](../adr/0021-dedicated-workload-account.md) left open:
- **The refresh-only apply:** the 4 refresh-drift entries accepted in precheck 8 are cleared by a state-only refresh. The `infra/org` plan now shows no changes and no drift.
- **The budget amounts:** the account-wide budget is $20 and the project budget is $40, as expected.
- **The cost allocation tag:** `project` is `Active`.

**Sanitized:** no account IDs, email addresses, ARNs, bucket names, budget names, or plan or state content. The other project is not named. Every step had its own approval. Every AWS call and Terraform command was made after an identity check: the management account's AdministratorAccess Identity Center session, not root.

## Why a refresh-only apply
- **The drift:** precheck 8 of phase 5 found 4 refresh-drift entries in the `infra/org` plan, each on a resource planned no-op:
  - the member account's root email, on the account, the organization and the organizational unit (changed through Account Management before phase 5);
  - `tags` null → {} on the read-only permission set (from phase 1c).
- **The deviation:** the user accepted it, valid only for exactly those entries.
- **The fix:** a refresh-only apply writes the refreshed values to the state and changes nothing in AWS.

## Preparation (read-only, 03:10Z)
| Check | Result |
|---|---|
| `main` (`7843888`) unchanged on GitHub and locally, a clean tree, 0 workflow runs in progress, 0 open PRs | pass |
| 5 GitHub variables. Of the 2 that name a role, both are in the member account, and none contains the management account's ID | pass |
| `scripts/role_trust_review.py`: 18 roles, 0 to review | pass |
| Sweep: 7 non-service roles and 1 user, admin-only | pass |
| Trusted access: exactly 2 services, IAM and Identity Center | pass |
| `infra/org` uses the management state bucket, key `org/terraform.tfstate` | pass |
| A private copy of the state, taken with `terraform state pull`: mode 600, git-ignored, serial 6, 22 managed resources | pass |

- **The plan:** `terraform plan -refresh-only -input=false -lock=false -out=…` exited 0. Plan SHA-256: `c3c3d842…2ca9`.
- **A private check of the plan,** read in memory with no JSON file written:
  - exactly the same 4 drift entries, with the same differing attributes;
  - the email values equal the `terraform.tfvars` value, compared privately;
  - 0 resource changes and 2 outputs unchanged;
  - no import, no deferred change, and no error.
- **Independent review:** an independent reviewer decoded the saved plan and confirmed the same. The state copy and the live state had the same serial and lineage.

## The apply (03:14Z)
**Pre-apply gates, all passed:**
- the plan hash;
- `main` unchanged and a clean tree;
- 0 runs and 0 open PRs;
- the sweep, admin-only;
- the identity;
- the live state still at serial 6, with the same lineage as the copy.

`AWS_PROFILE=ecp-admin terraform apply -input=false <plan>`, from `infra/org` with locking on: **`Apply complete! Resources: 0 added, 0 changed, 0 destroyed.`**

## Verification (read-only)
| Check | Result |
|---|---|
| The live state | serial 7, the same lineage, 22 managed resources |
| A fresh normal `infra/org` plan | exit 0, `No changes.`; **0 drift entries**; 22 resources and 2 outputs no-op |
| The three git-ignored `terraform.tfvars` files | unchanged (hashes compared) |

An independent reviewer re-checked this, read-only:
- serial 7, the same lineage, 22 managed resources;
- a fresh plan with no error, every change no-op, 0 drift, and outputs no-op.

## The budget amounts (read-only)
`budgets:DescribeBudgets` in the management account found 2 budgets (one page):
- the account-wide budget matches $20 USD;
- the project budget matches $40 USD.

No budget names or other values were printed.

## The cost allocation tag (read-only, after the user's go)
`aws ce list-cost-allocation-tags --tag-keys project`, run with `--no-paginate` and `AWS_MAX_ATTEMPTS=1`:
- **Requests:** exactly one Cost Explorer request, about $0.01. No more pages were returned.
- **Result:** the `project` tag is **`Active`**.

## Result
| Item open after phase 5 | Result |
|---|---|
| The 4 accepted refresh-drift entries | cleared: the `infra/org` plan has 0 drift |
| The $20 budget's amount | matches |
| The `project` cost allocation tag still `Active` | `Active` |

With these, every ADR-0021 phase 5 acceptance criterion is met.
