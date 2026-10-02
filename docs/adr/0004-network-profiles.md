# 4. Network profiles: NAT-free batch, temporary private demo stack

Date: 2026-10-02 · Status: accepted

## Context
A NAT gateway costs about $1 per day per gateway, plus data processing charges. The always-on batch only needs outbound HTTPS to EIA and access to S3. The demo stack needs private databases and admin access.

## Decision
**Batch profile (always on):**
- Lambda runs outside a VPC.
- The Fargate task runs in public subnets with an assigned public IP and a security group with no inbound rules.
- An S3 gateway endpoint (free) handles S3 traffic.
- No NAT gateway; a Rego policy enforces this.

**Demo-day profile (about 48 h, then destroyed):**
- Public subnets hold the ALB and one NAT gateway (not one per AZ).
- Private subnets hold the API tasks, RDS Postgres and ElastiCache Redis.
- Admin access goes through a small EC2 bastion with **no inbound rules**, reached by SSM Session Manager port forwarding to RDS.
- No SSH keys and no open port 22.

## Consequences
- The batch profile costs almost nothing for networking.
- A single NAT gateway is a single-AZ point of failure. That is acceptable for a demo; a production setup would use one per AZ.
- SSM needs the bastion's outbound path, through the NAT gateway or SSM interface endpoints. We use the NAT gateway.
