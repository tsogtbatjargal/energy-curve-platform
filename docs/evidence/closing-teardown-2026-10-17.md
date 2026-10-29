# Closing teardown: the Glue and batch stacks (2026-10-17)

The project is complete. After the temporary environment (2026-10-15) and the Redshift stack (2026-10-16) were destroyed, the Glue and batch stacks were torn down on 2026-10-17. Every apply was a saved plan whose SHA-256 was read independently and approved, run with the admin identity and locking on; every check below was read-only. No identifiers appear here.

## Why a prepare step
Both buckets are versioned and the batch ECR repository holds images, so Terraform cannot destroy them while non-empty. `force_destroy = true` (both buckets) and `force_delete = true` (the repository) were added for the teardown only (#104) and applied in place first. `glue_plan_check.py` and `batch_plan_check.py` gained `--change prepare` and `--change destroy`, built test-first with mutations proven.

## Glue (14 resources)
| Step | Result |
| --- | --- |
| Prepare plan | SHA-256 `632599aa…1c3b`: 1 update (the bucket's `force_destroy`, false to true), 13 no-ops, 0 drift |
| Prepare apply | 0 added, 1 changed, 0 destroyed; a fresh read-only plan showed 14 no-ops and 0 drift |
| Destroy plan | SHA-256 `03454766…093b`: 14 deletes, equal to the 14 managed state entries; the bucket held 16 object versions and 2 delete markers (49,892 bytes) |
| Destroy apply | 00:24:31 to 00:24:40 Toronto: 0 added, 0 changed, 14 destroyed |
| Checks | state empty; no job, no `ecp-glue-*` bucket, no role, no log group; the batch stack untouched |

## Batch (43 resources)
| Step | Result |
| --- | --- |
| Prepare plan | SHA-256 `97c0f230…a40d`: 2 updates (the bucket's `force_destroy` and the repository's `force_delete`, each false to true), 42 no-ops and 1 data read, 0 drift |
| Prepare apply | 0 added, 2 changed, 0 destroyed; a fresh plan showed 44 no-ops and 0 drift |
| Role kept | `terraform state rm aws_iam_service_linked_role.ecs` (serial 15 to 16); `AWSServiceRoleForECS` still exists in IAM |
| Destroy plan | SHA-256 `3f1bc9cd…1d28`: 43 deletes, equal to the 43 managed state entries, the role not among them; the bucket held 141 object versions and no delete markers (259,029 bytes), the repository 3 images, the schedule was enabled, and no execution or task was running |
| Destroy apply | 00:32:24 to 00:33:10 Toronto: 0 added, 0 changed, 43 destroyed |
| Checks (all zero) | state; schedule; state machine; Lambda function; active ECS cluster (the cluster reads INACTIVE); ECR repository; `ecp-data-*` buckets; the 10.42.0.0/16 VPC and any non-default VPC; network interfaces; NAT gateways; Elastic IPs; alarms; SNS topics; project log groups; the five `ecp-batch-*` roles |

## What remains
- The bootstrap stack in the workload account: the Terraform state bucket, the GitHub OIDC roles (`ecp-gha-plan`, `ecp-gha-deploy`), the workload permissions boundary and the break-glass role. The organization stack and the budgets are in the management account.
- Two service-linked roles, `AWSServiceRoleForECS` and `AWSServiceRoleForRedshift`, kept out of state because they are account-wide. A re-create of the batch or Redshift stack would first need `terraform import` of the role (`aws_iam_service_linked_role.ecs` or `.redshift`), since creating one that exists fails.
- The two CI service-image packages on GHCR, and the small state objects of the destroyed stacks.
- No AWS compute, storage of data, network or scheduler resource of the project remains; the expected monthly cost is about zero (IAM, Organizations and Budgets are free; the state bucket is a few kilobytes).

## CI after the teardown
`terraform-plan` now plans only bootstrap (the required `terraform-plan (bootstrap)` check keeps its name). The batch, Glue and Redshift plan jobs were informational and would have planned a full create (batch's would also fail on its image lookup once the repository is gone). `iac-static` still validates all stacks' source and Rego tests, and the plan checks and their tests remain in the repository as the record.

## Follow-up
The final cost figure (read 2026-10-28, still estimated: gross usage $8.35, credits -$8.35, net $0.00) is in [the M6 note](m6-temporary-environment-2026-10-15.md#cost).
