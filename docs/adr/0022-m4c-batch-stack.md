# 22. M4c: the batch stack, applied in two stages

Date: 2026-10-08 · Status: **accepted** (2026-10-10, after the first scheduled run). Evidence: [m4c-2026-10-09](../evidence/m4c-2026-10-09.md), [m4c-scheduled-2026-10-10](../evidence/m4c-scheduled-2026-10-10.md). The CI plan job and the deployment inputs below are coded (M4c-2, part A); the publish workflow is not.

## Context
M4a (the S3 store, ADR-0019) and M4b (the entry points and image, ADR-0020) are done. M4c is what remains: the `infra/batch` stack in the `ecp-workloads` member account (ADR-0021), the first image, and the first cloud run. Preconditions met: PLAN.md R1 (ADR-0017) and R2 (ADR-0018, re-accepted in the member account). The cloud runs synthetic data only, so R3 matters only for M6.

**The image cannot exist before the first apply:** the member account has no ECR repository, and the repository is part of this stack. A Lambda function or task definition that names an image needs that image to exist. So the stack is applied in two stages.

Verified read-only in the member account (2026-10-08): 0 ECR repositories; no `AWSServiceRoleForECS`; a Lambda concurrency limit of 10, all unreserved.

## Decision

### Two stages, one stack, one variable
`image_digest` (default `""`) is validated as `""` or `^sha256:[0-9a-f]{64}$`. Everything that runs the image has `count = local.stage2 ? 1 : 0`.

| Stage | `image_digest` | Creates |
|---|---|---|
| 1 | `""` | The data bucket and its companions; the ECR repository with its lifecycle and repository policies; the VPC (2 public subnets, internet gateway, route table, S3 gateway endpoint, the default security group emptied, a task security group with HTTPS egress and no ingress); the ECS service-linked role and the Fargate cluster; the five roles and their inline policies; the two log groups; the alerts topic and its email subscription. 39 resources |
| 2 | the pushed image's digest | The Lambda function and the ECS task definition, both on `<repository URL>@<digest>`; the state machine; the schedule, **disabled**; the failure alarm. 5 resources, plus a `data "aws_ecr_image"` lookup that fails the plan unless the digest is in the repository |

- **All IAM is in stage 1.** Policies name their targets by ARNs built from fixed names, the account and the region, so they are known at plan time in both stages (R1) and stage 2 changes no IAM.
- **A third plan** later sets `schedule_enabled = true` (one in-place update, which also moves the schedule to the universal target, see below), after the first manual run and its replay pass.

### The pieces
| Piece | Design |
|---|---|
| State | The member state bucket, key `batch/terraform.tfstate`, `use_lockfile`; a git-ignored `backend.hcl` |
| Account guard | `expected_account_id`; postconditions that the caller is that account and is not the organization's management account |
| Data bucket | `terraform-aws-modules/s3-bucket/aws` at exactly 5.16.2. SSE-S3, versioning on, owner-enforced, public access blocked, deny non-TLS. Lifecycle: `store/staging/` expires after 7 days; old versions after 30 days; incomplete uploads aborted after 1 day. The store is `s3://<bucket>/store` |
| ECR | `ecp-batch`: immutable tags, scan on push, AES256, keep the 2 newest images (the count rises to 5 with the publish workflow) |
| Lambda (`cloud.lambda_handler`) | Image by digest, no VPC, 512 MB, 60 s. **No reserved concurrency** (below). Role `ecp-batch-stage`: `s3:PutObject` on `store/staging/*`, and its own log stream |
| Fargate (`cloud.task_main`) | 0.5 vCPU, **1 GB** (below). Entry point `python -m energy_curves.cloud`; the state machine passes `task <run_id>`. Role `ecp-batch-task`: get and put under `store/`, and `s3:ListBucket`, so a missing key is a 404 rather than a 403. Role `ecp-batch-exec`: the ECR pull for this repository and its own log stream |
| Network (ADR-0004 batch profile) | Public subnets, a public IP for the task, egress 443 only, no ingress. No NAT gateway, no interface endpoints (a Rego rule now enforces this outside the demo stack) |
| Step Functions (Standard) | `Stage` (Lambda invoke, 2 retries with full jitter) → `Pipeline` (`ecs:runTask.sync`) → `ExitCode`, a Choice that succeeds only on exit code 0, else `PipelineFailed`. 15-minute timeout. Role `ecp-batch-sfn`: invoke the function; run the task family only in this cluster; describe and stop its tasks; the managed `.sync` rule; pass only the two task roles, only to ECS tasks |
| Scheduler | Daily at 12:00 America/Toronto, created **disabled**. Role `ecp-batch-scheduler`: `states:StartExecution` on this state machine only |
| Alarm | One alarm on `SUM(ExecutionsFailed, ExecutionsTimedOut, ExecutionsAborted)` > 0, missing data not breaching, to the `ecp-batch-alerts` topic. One email subscription; the address is set only in the git-ignored `terraform.tfvars`, never in the repo, and is not sensitive. It stays pending until confirmed |
| Tags | Provider `default_tags`, `stack = batch` |

**Trust policies** each name one service. The ECS, Step Functions and Scheduler trusts add `aws:SourceAccount`; the ECS trusts add `aws:SourceArn` for the account's ECS resources, and the Step Functions trust for this state machine. The Lambda trust is AWS's documented execution-role trust, with no condition.

### Why not the community modules for Lambda, ECS, Step Functions and the VPC (ADR-0008)
ADR-0008 prefers `terraform-aws-modules`. Here only the bucket uses one:
- Those modules create IAM roles and policies internally by default. Every role here must be one of the five reviewed templates with the boundary, checked by exact address before apply.
- The stage checks pin the exact resource set by address.

The bucket module creates no IAM, and its companions are the ones ADR-0006's storage rule checks.

### No Lambda reserved concurrency
The member account's limit is 10 concurrent executions, and AWS keeps at least 10 unreserved, so any reservation fails. None is needed:
- the state machine is the only caller, once a day;
- overlapping runs are safe through ADR-0019's conditional pointer write.

### The ECS service-linked role
ECS creates `AWSServiceRoleForECS` itself on `CreateCluster` when the caller may `iam:CreateServiceLinkedRole` (ECS Developer Guide, "Using service-linked roles for Amazon ECS"). It is declared instead (`aws_iam_service_linked_role`), so it is in the plan and the stage-1 check. The cluster has `depends_on` on it: otherwise Terraform could create both in parallel, `CreateCluster` would create the role first, and the explicit create would fail because the name is taken. It cannot carry a boundary, and R2's rule covers `aws_iam_role` only.

A full teardown would try to delete it, which AWS refuses while ECS resources use it.

### The ECR repository policy
Lambda Developer Guide, "Amazon ECR permissions" (`images-create.html`):
- In the same account, "only one side needs to allow access": the execution role or the repository policy, with `ecr:BatchGetImage` and `ecr:GetDownloadUrlForLayer`.
- "If the Amazon ECR repository does not include these permissions, Lambda attempts to add them automatically", which works only if the caller of `CreateFunction` has `ecr:SetRepositoryPolicy`.

The stage role deliberately has no ECR access, so without a repository policy the admin's `CreateFunction` would make Lambda write one itself: an out-of-band change to a stack resource. So stage 1 declares it:
- the `lambda.amazonaws.com` principal and the 2 pull actions, under AWS's own Sid, `LambdaECRImageRetrievalPolicy`;
- `ArnLike aws:sourceARN` = `arn:aws:lambda:<region>:<account>:function:*`.

**The condition is AWS's documented form, and nothing narrower.** *(Corrected 2026-10-08: the merged ADR said the condition was this function's exact ARN, "the condition key AWS's own example uses". That overstated it. The guide's only conditioned example is the cross-account statement, and its value is `function:*`; the same-account minimum policy has no condition.)*
- **No exact function ARN:** nothing documents one, and nothing says whether Lambda's pull sends the plain function ARN or a qualified one (`…:ecp-batch-stage:$LATEST` or a version). An exact value would not match a qualified ARN, and the function could then enter `Failed` when Lambda next fetches the image.
- **No `aws:SourceAccount`:** nothing documents that Lambda sends it with this pull.
- **What the wildcard costs:** any Lambda function in this account and region may pull from this repository. The account is single-purpose, and same-account IAM already lets any principal there with ECR permissions pull, so the loss is small. `tests/test_batch_iam.py` pins the statement and its matching.

A diff on this policy in the re-plan after stage 2 would mean Lambda rewrote it: stop.

### The idempotency key and the replay check
- **The pipeline's idempotency key is `logical_input_id`** (ADR-0012, ADR-0019): a hash of the source, the sorted requests, every staged page's SHA-256 and the transform version.
- **`run_id`, the execution name, only names `staging/<run_id>/`.**
- **A Standard execution name** is unique per state machine. The same name and input while it runs is idempotent. After it closes, or with other input, `StartExecution` returns `ExecutionAlreadyExists`, and the name can be reused 90 days after it closes (StartExecution API reference).
- **So a new execution is never a replay.** It re-stages with a new retrieval time, giving a new `logical_input_id` and a watermark-only version (ADR-0020).
- **The replay check** re-runs only the Fargate step on the same `run_id`: one `ecs run-task` with `task <run_id>`. The staged pages are the same, so the result must be `already_published` with the pointer unchanged. This is what a retry of the ECS step would do.
- **The schedule names its executions.** The AWS pages read for this decision (2026-10-09) do not state what name the templated Step Functions target passes to `StartExecution`, so the schedule moves to the **universal target** `arn:aws:scheduler:::aws-sdk:sfn:startExecution`:
  - Its input is `{"StateMachineArn":"<this state machine>","Name":"<aws.scheduler.execution-id>","Input":"{}"}`. `Name` is a `StartExecution` request parameter (1 to 80 characters, unique per state machine); the context attribute `<aws.scheduler.execution-id>` is "the unique ID that EventBridge Scheduler assigns for each attempted invocation of a target", shown as `d32c5kddcf5bb8c3`.
  - The docs give that ID only by example, not a format or a length. If it ever fails `^[A-Za-z0-9_-]{1,80}$`, the stage and the task refuse the `run_id` before any write, the execution fails and the alarm fires.
  - A retried invocation has a new ID, so a duplicate execution is possible if a delivery was accepted but reported as failed. It is harmless: idempotency is keyed on the logical input, not the run_id (see above).
  - The input is written with `format()`, not `jsonencode`, which would escape `<` and `>` and leave Scheduler no keyword to replace. A Terraform test and the plan check pin the exact bytes.
  - The role keeps `states:StartExecution` on this state machine only: the universal-target page requires "the permissions to call the API operation you want your schedule to target", and that operation is `StartExecution`. Plan 3's first run confirms it.

### Exit codes
`quarantined` exits 3 (ADR-0020). The Step Functions ECS integration page does not say whether `runTask.sync` fails on a non-zero container exit. So `ResultSelector` takes `Containers[0].ExitCode`, and the Choice state fails the execution on anything but 0. A missing exit code fails the state with a runtime error.

### Fargate memory, measured (2026-10-08)
Locally, with rootless podman:
- the image built from `main`;
- synthetic data, a file store, no network for the task;
- a 2 GB cgroup limit;
- `memory.peak` read inside the container after the run.

| Run | Result | cgroup peak | Page cache at the end |
|---|---|---|---|
| 1, empty store | `published`, version 1, 18 rows | 96.7 MiB | 48.5 MiB |
| 2, next day, store holds version 1 | `published`, version 2, 2 new rows | 49.0 MiB | 0.06 MiB |

- **Run 1's peak includes about 48.5 MiB of page cache** (reading the interpreter and libraries for the first time). Run 2 found them cached, charged to run 1's cgroup, so its peak is close to the process's own memory.
- **The task gets 1024 MiB,** the smallest Fargate size for 0.5 vCPU and about 10 times the larger peak.

### Gates for each stage
| Gate | Stage 1 | Stage 2 |
|---|---|---|
| `policy_gate.py --stack batch` (ADR-0006 rules, R1, R2) | yes | yes |
| `batch_plan_check.py` | `--stage 1`: from an empty state, exactly the 39 creates with their reviewed settings; every role and policy equals its template for this account, known at plan time; every role has the boundary | `--stage 2 --digest <approved>`: every stage-1 resource unchanged (so no IAM change); exactly the 5 creates; the digest found in the repository at plan time; both steps on that digest, synthetic only, with no key; no reserved concurrency; the schedule disabled; the alarm on the topic. `--stage 3 --digest <approved>`: every other resource unchanged; exactly one in-place update of the schedule, only its state (to ENABLED) and its target (the universal target and the exact input above); `--stage 4 --digest <approved>` (a rollout): exactly the function updated in place (the image only), the task definition replaced for one reason (`container_definitions`, a new revision) and the state machine updated in place (its definition, unknown at plan time, names the new task-definition ARN); every other resource unchanged, so no IAM, schedule or alarm change; the new settings equal the reviewed ones on the approved digest |

`batch_plan_check.py` also checks, for both stages:
- a sound plan, with no output change;
- `ca-central-1`, the expected account, and not the management account;
- no excluded resource and no ingress;
- the cluster's `depends_on`;
- the bucket module pinned;
- no override file or JSON configuration in the source.

| Other gates | Stage 1 | Stage 2 |
|---|---|---|
| R2 offline (`tests/test_batch_iam.py`) | in CI | — |
| R2 in AWS: `simulate-custom-policy` per role with the boundary (read-only) | before the apply | — |
| R2 in AWS: `simulate-principal-policy` on each real role | after the apply | — |
| `role_trust_review.py` | after the apply | — |
| trivy config (CI); trivy image on the pushed digest | config | image |

**Three plan hashes** are approved separately: stage 1, stage 2, and enabling the schedule. The local image push (by digest), the one manual `StartExecution`, the one replay `ecs run-task` and the subscription confirmation are separate approvals, not plans.

### Synthetic only, no secret
- **The image** sets `ECP_SOURCE=synthetic`; the entry points refuse anything else (ADR-0020).
- **Both steps** pin `ECP_SOURCE=synthetic` and get only the store URL besides.
- **The stack** has no Secrets Manager secret, SSM parameter or KMS key, no key or secret variable, and no sensitive variable.
- **No private values in the repo:** no account ID or email address but placeholders.

`tests/test_batch_source.py` enforces this, and the stage-2 check rejects any other environment.

### Cost (`ca-central-1`, AWS public price list, 2026-10-08)
About **$0.15 a month**, or $0.33 with no free tier (the ECR line below was corrected on 2026-10-09 from images assumed at 0.9 GB; the original estimate was $0.27 and $0.45; keeping 5 images instead of 2, planned with the publish workflow, adds at most $0.10):

| Item | Monthly |
|---|---|
| Fargate (0.5 vCPU, 1 GB, a few minutes a day) | about $0.06 |
| The task's public IPv4 address while it runs | about $0.01 |
| S3 | about $0.01 |
| ECR, at most 2 images of about 0.32 GB (measured: `imageSizeInBytes` of the pushed image, stored compressed; about $0.03 a month each) | at most $0.06 |
| The alarm (3 metrics) | $0.00 to $0.30 |
| Lambda, Step Functions, Scheduler and logs | within the free tiers |

Stage 1 alone costs cents: storage only, nothing runs.

**Excluded:**
- a NAT gateway;
- interface endpoints;
- KMS keys;
- VPC flow logs;
- Container Insights;
- an always-on service.

The trivy findings these exclusions raise are each ignored with the reason in the source.

### The steps after this ADR (each separately approved)
1. **This code,** merged.
2. **Read-only prechecks in the member account:**
   - identity;
   - the admin-only sweep;
   - the trust review;
   - still 0 repositories, no `batch/` state key and no ECS service-linked role;
   - the R2 simulations;
   - Lambda limits unchanged.
3. **Plan 1:** the gates, then the hash. Stop.
4. **Apply plan 1,** then verify (R2 post-apply, the trust review, the sweep, a no-change re-plan), and confirm the subscription.
5. **Build from `main`,** scan with trivy, and push by digest.
6. **Plan 2 with that digest:** the gates, then the hash. Stop. Then apply and verify, including a no-change re-plan.
7. **One manual execution** (expect `published`), then one replay `ecs run-task` (expect `already_published`, pointer unchanged).
8. **Plan 3** enables the schedule and moves it to the universal target, then the first scheduled run is watched (name, success, alarm).
9. **The evidence,** then the CI plan job (done in code, see below), and later the publish workflow and a stage-4 rollout check, which are not built yet.

### The deployment inputs are committed, and CI plans the stack (M4c-2, part A, 2026-10-09)
- **`infra/batch/image.auto.tfvars` is tracked** (the one exception to the `*.tfvars` ignore rule) and holds exactly `image_digest = "<the deployed digest>"` and `schedule_enabled = true`. A plain plan now describes the deployed state. **A rollout is a PR that changes the digest line, and a rollback is a revert.** A `-var` on the command line still wins, so a stage-1-style plan needs `-var image_digest=`. The tests pin that the file holds only those two assignments and is not ignored; the Terraform tests set their own inputs, because `terraform test` loads this file too.
- **The alert address stays out of CI.** The subscription has `lifecycle { ignore_changes = [endpoint] }`, and CI plans with `alert_email = "ci@example.invalid"`. An email subscription's endpoint cannot change in place, so **changing the address later is a deliberate replace step** (`terraform apply -replace=aws_sns_topic_subscription.email`, which leaves the new subscription pending confirmation), not something a plan does by accident. Nothing reads the live subscription from CI.
- **The ECR lifecycle keeps 2 images for now.** The count goes up to **5 together with the publish workflow**, as part of that work, so that a publish or two cannot expire the deployed image before a rollout: changing it replaces the lifecycle policy (delete, then put; the provider reports `cannot_update`), which needs its own reviewed plan and a check mode that accepts exactly that replace. The digest guard (`data.aws_ecr_image`) already fails a plan whose digest is not in the repository; a rollout check will also confirm it.
- **The CI job** (`terraform-plan`, `batch` added to the matrix) assumes the member plan role, plans with `-lock-timeout=60s`, and prints only the `Plan:` / `No changes` line (a failed plan prints the first line of each error with 12-digit numbers masked). The plan and its JSON go to files that are removed afterwards. It then runs the policy gate with `--stack batch --stack-dir .`. It is **informational**: not a required check, so an `infra/batch` change merged but not yet applied shows as a change without blocking.
- **Steady state:** with these inputs a plan of the real state shows `No changes` (read-only, streamed JSON, 2026-10-09). CI's steady state could later get a "current" check mode (all resources no-op); it is not built.

**Stop rules:** any gate not OK; a stage-2 plan that changes a stage-1 resource or any IAM; a digest other than the approved one; any excluded resource; more than about $1 without a fresh go; a failed first run or replay. Stop and report, with no retry, import or workaround.

## Rolling out a new image (M4c-3, 2026-10-10)
- **The shape was read first** with a read-only plan of the rebuilt image's digest (`-lock=false`, the plan held in an anonymous in-memory file, nothing on disk): `Plan: 1 to add, 2 to change, 1 to destroy`, 41 resources unchanged, no drift.
  - The function: update in place (`image_uri`; `last_modified` is computed).
  - The task definition: replace, `replace_paths = [[container_definitions]]`, `delete` then `create` (a new revision, the old one deregistered; no execution is in flight when it is applied).
  - The state machine: update in place; its definition is unknown because it names the new task-definition ARN.
  - The `before` side of the task definition carries the provider's read-back values (empty maps and strings where the configuration has none); the check reads only the `after` side.
- **`batch_plan_check.py --stage 4`** accepts exactly that shape and was run against the real plan: no problems. Its tests are written to the same shape, including all-string variables.
- **`image.auto.tfvars` now names the new digest.** The file says what is deployed, so between the merge of the digest PR and the apply, the informational CI plan job shows these three changes; that is the PR the apply follows. A rollback is a revert of that PR followed by the same check with the old digest, which the stage-4 check accepts the same way (the old image stays in ECR while the lifecycle keeps two).

## Consequences
- **Two applies instead of one,** in exchange for never naming an image that does not exist, and a stage-2 plan that cannot change IAM.
- **The stack depends on one registry module,** pinned exactly and checked in both the plan and the source.
- **Verified in the first applies** (an independent reviewer repeated the key checks read-only):
  - **The service-linked role took the provider's tags** at the stage-1 apply: `39 added, 0 changed, 0 destroyed`, no tagging error.
  - **`CreateFunction` left the ECR repository policy untouched:** the re-plans after the stage-2 apply show no diff on it.
  - **The log groups received streams with events:** the function's group has 1 stream with 3 events; the task's group has 2 streams (the run and its replay) with 2 events each. This is the real proof for the two R2 scenarios the IAM simulator cannot evaluate (a slash-prefixed log group; see the evidence).
  - **The first run** `published`, and its replay returned `already_published` with the pointer unchanged.
  - **The universal target, at the first scheduled run (2026-10-10), now verified:** `StartExecution` accepted the `Name` in the input; the execution ID is a UUID (36 characters), which fits `^[A-Za-z0-9_-]{1,80}$` and differs from the manual run's name; the scheduler role needed nothing beyond `states:StartExecution`. The run succeeded and published version 2 as a watermark-only version (see the evidence). That the ID differs from day to day is confirmed on the second scheduled run.
- **Learned in the applies:** the provider's read-back after a create leaves 18 (stage 1) and 4 (stage 2) refresh-drift entries, all null → empty values, which two reviewed refresh-only applies recorded. A stage-2 gate that refuses drift needs them recorded first; drift is judged from the plan JSON, because the text output showed only 1 of the 18.
- **Not verified by a run:**
  - whether `runTask.sync` alone fails on a non-zero exit: the Choice state makes this moot.
