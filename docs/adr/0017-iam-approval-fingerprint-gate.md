# 17. R1: approval fingerprints for IAM policies unknown at plan time

Date: 2026-10-03 · Status: accepted

## Context
PLAN.md R1 requires that, before any workload stack is applied, an IAM policy whose JSON is unknown at plan time fails the gate unless a reviewed approval matches a fingerprint of the policy, its dependencies and its inputs. ADR-0006 left such policies as a warning, which was acceptable for the bootstrap stack only.

Verified on 2026-10-03 with a real `terraform show -json` (Terraform 1.15.8, hashicorp/aws ~> 6.67) of the fixture stack `tests/fixtures/iam_gate/stack`:
- `policy = jsonencode({... Action = ["s3:PutObject"] ...})` appears only as `{"references": ["aws_s3_bucket.data.arn", "aws_s3_bucket.data"]}`. The actions are gone.
- `policy = data.aws_iam_policy_document.read.json` appears only as that reference. The statements are in the data source's own `expressions`.
- Locals are absent. A reference to one appears only as `local.<name>`.
- Terraform carries sensitivity marks into unknown values: a policy built from a sensitive variable through a local has `after_sensitive.policy = true`.
- Module calls carry `source`, `version_constraint` and input `expressions`. `.terraform/modules/modules.json` holds the resolved registry version.
- **`relevant_attributes` lists every referenced attribute, known or unknown.** It cannot tell which values are unknown by itself. Unknown values are visible in each resource's `change.after_unknown`.

The fixture plans offline with dummy credentials, because `aws_iam_policy_document` is evaluated locally. An opt-in test (`ECP_TERRAFORM_FIXTURE=1`) re-plans it and compares the result with the committed plan JSON.

## Decision

### Where it runs
- **One gate.** `scripts/policy_gate.py` (conftest, ADR-0006) now also runs `scripts/iam_approval.py`.
- **`--stack` is required,** so the R1 check cannot be skipped by omission. CI passes the matrix stack name.
- **One dependency:** `python-hcl2`, to read locals from the source (below). It is the `gate` dependency group, the only one CI's terraform-plan job installs.
- **Stacks.**
  - `bootstrap` is exempt (notes only): it creates no workload roles, and its policies are reviewed with ADR-0010.
  - Every other stack is a workload stack.
- **Policy attributes checked:**
  - `policy` on `aws_iam_policy`, `aws_iam_role_policy`, `aws_iam_user_policy` and `aws_iam_group_policy`;
  - `assume_role_policy` on `aws_iam_role`.
- **Which resources.** As in ADR-0006, only exact `["delete"]` and `["forget"]` plans are skipped.

### The fingerprint
It is `sha256:` plus the SHA-256 of the canonical JSON (sorted keys, no whitespace) of:

1. **Identity:** format `ecp-iam-approval-v1`, stack, address, resource type, attribute.
2. **Closure:** from the attribute's `references`, transitively through `configuration`:
   - every resource and data source reached, with its full `expressions` and count/for_each expressions;
   - every module call reached, with `source`, `version_constraint` and all input expressions, which are followed in the caller's scope;
   - module output expressions, followed in the module's scope;
   - variable configurations;
   - every local reached. Plan JSON omits locals, so the gate parses the module's `*.tf` files with `python-hcl2` (`*.tf.json` as JSON) and follows every reference in the local's expression, in the same scope. A reference inside a string literal is followed too, which can only add to the closure. A local the gate cannot find or parse fails it ("run terraform init"). The local's text is covered by item 4.
3. **Inputs:** plan `variables` values for root variables in the closure, and the registry source and resolved version of every registry module in the closure.
4. **Source:** SHA-256 of every `*.tf`/`*.tf.json` in the stack root, plus `.terraform.lock.hcl`, plus every module directory in the closure. That always includes the policy's own module and its ancestors, which hold its `jsonencode` literals even when nothing it references leads back to them. That includes **registry modules' downloaded copies**, which goes beyond the PLAN text: a moved or re-published version tag would otherwise change a policy under an existing approval. A missing lock file, `modules.json` entry or module download fails the gate ("run terraform init").

Editing any root stack file invalidates every approval in that stack, which is conservative by design. A local-module edit invalidates only the policies whose closure reaches it.

### Gate rules
- **Sensitive values.** A sensitive variable anywhere in the closure fails outright, even with an approval, and so does a policy Terraform marks sensitive (`after_sensitive`), for example through `sensitive()` in a local. Its value is never hashed.
- **Unknown values.** Every unknown value the policy depends on must be the `arn`, `id` or `name` of a managed resource in the same stack. This is checked through each referenced attribute's `after_unknown`, because `relevant_attributes` cannot tell known from unknown.
  - An unknown attribute of any data source other than the locally evaluated `aws_iam_policy_document` also fails, for example an identity looked up at apply time.
- **Approvals file.** `policy/approvals/iam_unknown.json` is `{"approvals": [...]}`. Each entry has exactly these fields:
  - `stack`, `address`, `attribute`;
  - `fingerprint` (`sha256:<64 hex>`);
  - `reviewer`, `approved_on`, `expires_on` (ISO dates, expiry 0–30 days after approval);
  - `reason`.

  An unknown or missing field, a malformed value, or a duplicate entry fails the whole gate.
- **Matching.** A policy passes only with an entry for the same stack, address and attribute whose fingerprint equals the recomputed one, from its approval date to its expiry date inclusive (UTC). A future-dated approval does not count yet.
- **No plan values in output.** The gate prints addresses, attribute names, file paths and fingerprints only, never plan values (plan JSON can hold secrets in plain text).

### Approval flow
1. The gate's failure line shows the policy's fingerprint. Locally: `python scripts/iam_approval.py fingerprint plan.json --stack S --stack-dir infra/S --address ADDR`.
2. The reviewer reads the source diff of the closure files against the checklist in PLAN.md R1, then commits the entry in an approval PR. That file sits outside every stack, so the approval does not invalidate itself.
3. The workload apply job (M4) must run this gate on the very plan file it applies, inside the protected `prod` environment.

## Consequences
- **R1 is met.** An unreviewed or changed IAM policy cannot reach a workload apply, and approvals expire within 30 days.
- **Re-approval is cheap, and expected,** after any edit to a stack.
- **Tests.** `tests/test_iam_approval.py` covers every case in PLAN.md R1 against the real fixture plan, plus:
  - changed registry module code under the same version, with the real `cloudposse/label` 0.25.0 files vendored as the downloaded copy, so the locals parser runs on third-party HCL;
  - sensitive variables, unknown values and variable values reached through locals;
  - a policy inside a module;
  - future-dated approvals;
  - local-module scoping;
  - deletions;
  - missing downloads;
  - approvals-file validation;
  - the CLI never printing values.

  Negative controls confirmed the guards. Disabling source hashing, closure following, the sensitive rule, the unknown-value rule, expiry, registry-file hashing, local following, the containing-module hash, the sensitivity-mark check, or the approval-date check each fails its tests.
