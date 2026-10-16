# M6: the temporary environment, built and destroyed (2026-10-15)

PLAN.md M6 lists an optional temporary environment. It was built on 2026-10-13, used, and destroyed on 2026-10-15. Everything below was read from AWS or from the run records; no real price data was involved.

## What it was
- **Diagram:** [`docs/diagrams/demo.png`](../diagrams/demo.png) shows it: the request path from the owner address through the load balancer to the API, the Session Manager admin path, and the outbound traffic through the one NAT gateway.
- **Purpose:** the local serving stack (FastAPI, Postgres, Valkey) running in the workload account for a short time: an internet-facing load balancer on HTTP:80 limited to one owner address (a /32), the API on Fargate in private subnets, a private single-AZ RDS Postgres, a private ElastiCache Valkey with TLS, a NAT gateway for outbound traffic, a private SSM bastion with no public address, and the batch stack's ECR image by digest. Every role carried the workload permissions boundary.
- **Data:** synthetic only (ADR-0011).
- **Code:** its Terraform is kept outside this repository, so no resource of it appears in `infra/`.
- **Stacks it did not touch:** batch, Glue, Redshift and bootstrap.

## Built (2026-10-13)
- **First apply:** the ElastiCache replication group failed to create because the ElastiCache service-linked role was still propagating. The other resources were created.
- **Completion:** a second apply created the group and finished, and read-back drift (tags and the cache's user groups) was recorded with refresh-only applies.
- **Verified:** `/api/health` returned 200 and the load balancer's target was healthy.

## Loaded
One bootstrap task loaded the synthetic data: 278 observations, dataset version 1. It ran from a temporary task definition in its own family; the API service kept a read-only filesystem.

## Destroyed (2026-10-15)
- **Plan:** `plan -destroy` showed 41 deletes and nothing else: no create, update, replace, drift or deferred change, and no address from another stack. A destroy-mode plan check accepted exactly the environment's resources, and the policy gate, as expected for an all-delete plan, reported that it had nothing to evaluate.
- **Apply:** 41 destroyed, about 11.5 minutes, no error.
- **Read-only checks afterwards, all zero:** managed resources in state, load balancers and target groups, NAT gateways, Elastic IPs, RDS instances and snapshots, ElastiCache groups, the bastion instance, the VPC and its network interfaces, the ECS cluster, RDS-managed secrets, and the log group. The batch, Glue and Redshift resources and the ECR repository were still present.
- **Last step:** the temporary bootstrap task definition's one active revision was deregistered.

## Cost
Cost Explorer read on 2026-10-16 showed net $0.00 per day, estimated; its data lags and credits or free tier may offset charges, so this is preliminary. The planned cost was about $7.4 for 48 hours. A final figure will be added once billing settles.
