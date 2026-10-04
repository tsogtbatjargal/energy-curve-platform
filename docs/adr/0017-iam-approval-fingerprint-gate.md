# 17. R1: approval fingerprints for IAM policies unknown at plan time

Date: 2026-10-03 · Status: accepted

## Context
PLAN.md R1 requires that, before any workload stack is applied, an IAM policy whose JSON is unknown at plan time fails the gate unless a reviewed approval matches a fingerprint of the policy, its dependencies and its inputs. ADR-0006 left such policies as a warning, which was acceptable for the bootstrap stack only.

Verified on 2026-10-03 with a real `terraform show -json` (Terraform 1.15.8, hashicorp/aws ~> 6.67) of the fixture stack `tests/fixtures/iam_gate/stack`:
- `policy = jsonencode({... Action = ["s3:PutObject"] ...})` appears only as `{"references": ["aws_s3_bucket.data.arn", "aws_s3_bucket.data"]}`. The actions are gone.
- `policy = data.aws_iam_policy_document.read.json` appears only as that reference. The statements are in the data source's own `expressions`.
- Locals are absent. A reference to one appears only as `local.<name>`.
- **A whole object is indistinguishable from one of its attributes.** For `aws_s3_bucket.data.arn`, Terraform lists both `aws_s3_bucket.data.arn` and `aws_s3_bucket.data`, and for `module.logs.group_arn` it lists `module.logs` too. So `lookup(aws_s3_bucket.data, "k")`, or `jsonencode(module.logs)` next to an ARN, leaves the plan's references unchanged.
- Terraform carries sensitivity marks into unknown values: a policy built from a sensitive variable through a local has `after_sensitive.policy = true`.
- Module calls carry `source`, `version_constraint` and input `expressions`. `.terraform/modules/modules.json` holds the resolved registry version.
- **`relevant_attributes` lists every referenced attribute, known or unknown.** It cannot tell which values are unknown by itself. Unknown values are visible in each resource's `change.after_unknown`.

The fixture plans offline with dummy credentials, because `aws_iam_policy_document` is evaluated locally. An opt-in test (`ECP_TERRAFORM_FIXTURE=1`) re-plans it and compares the result with the committed plan JSON.

## Decision

### Where it runs
- **One gate.** `scripts/policy_gate.py` (conftest, ADR-0006) now also runs `scripts/iam_approval.py`.
- **`--stack` is required,** so the R1 check cannot be skipped by omission. CI passes the matrix stack name.
- **One dependency:** `python-hcl2`, to read the source (below). It is the `gate` dependency group, the only one CI's terraform-plan job installs.
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
2. **Closure:** from the attribute's `references` and the resource's own `count_expression`/`for_each_expression`, transitively through `configuration`. The policy itself references only `each.value` or `count.index`; their values come from the meta-argument. The closure covers:
   - every resource and data source reached, with its full `expressions` and count/for_each expressions;
   - every module call reached, with `source`, `version_constraint` and all input expressions, which are followed in the caller's scope;
   - module output expressions, followed in the module's scope;
   - variable configurations;
   - every local reached.

   **The source is scanned too.** Plan JSON omits locals and hides whole-object uses (above). So the gate also parses each module's `*.tf` files with `python-hcl2` (`*.tf.json` as JSON) and scans the source of:
   - the policy attribute, and the resource's own `count` and `for_each`;
   - every resource, data source, module call and output reached, except meta-arguments such as `depends_on`;
   - every local reached.

   **How the scan reads references.** It finds every traversal wherever it starts, including inside an index (`local.m[var.k]` reaches `var.k`). A traversal that stops before an attribute (`aws_s3_bucket.data`, `data.t.n`, `module.m`) is a whole-object use. For a module, that means every output. A reference inside a string literal is followed too, which can only add to the closure.

   **Fails closed.** A block or local the gate cannot find or parse fails it ("run terraform init"). Their text is covered by item 4.
3. **Inputs:** plan `variables` values for root variables in the closure, and the registry source and resolved version of every registry module in the closure.
4. **Source:** SHA-256 of every `*.tf`/`*.tf.json` in the stack root, plus `.terraform.lock.hcl`, plus every module directory in the closure. That always includes the policy's own module and its ancestors, which hold its `jsonencode` literals even when nothing it references leads back to them. That includes **registry modules' downloaded copies**, which goes beyond the PLAN text: a moved or re-published version tag would otherwise change a policy under an existing approval. A missing lock file, `modules.json` entry or module download fails the gate ("run terraform init").

Editing any root stack file invalidates every approval in that stack, which is conservative by design. A local-module edit invalidates only the policies whose closure reaches it.

### Gate rules
- **Sensitive values.** A sensitive variable anywhere in the closure fails outright, even with an approval, and so does a policy Terraform marks sensitive (`after_sensitive`), for example through `sensitive()` in a local. Its value is never hashed.
- **Unknown values.** Every unknown value the policy depends on must be the `arn`, `id` or `name` of a managed resource in the same stack. This is checked through each referenced attribute's `after_unknown`, because `relevant_attributes` cannot tell known from unknown.
  - A whole-object use counts as using every unknown attribute of that object (nested unknowns included). So `lookup(aws_s3_bucket.data, "website_endpoint")` fails, even next to `aws_s3_bucket.data.arn`.
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
  - sensitive variables, unknown values and variable values reached through locals, including inside a dynamic index;
  - whole-resource and whole-module uses, alone, mixed with an ARN or output, and through a local; `depends_on` not counted;
  - the policy's own `for_each` (a tfvars value, through a local, an unknown value, a whole-object lookup) and `count`;
  - missing blocks and `*.tf.json` sources;
  - a policy inside a module;
  - future-dated approvals;
  - local-module scoping;
  - deletions;
  - missing downloads;
  - approvals-file validation;
  - the CLI never printing values.

  Negative controls confirmed the guards. Disabling source hashing, closure following, the sensitive rule, the unknown-value rule, expiry, registry-file hashing, local following, index scanning, whole-object detection, the policy-attribute scan, the meta-argument filter, the policy's own `for_each`/`count` (plan-side follow and source scan removed together; the source scan alone, via a whole-object lookup), the containing-module hash, the sensitivity-mark check, or the approval-date check each fails its tests.
