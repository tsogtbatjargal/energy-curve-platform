# M4c (ADR-0022): the batch stack's two stages, planned and applied against a mocked AWS provider.
# Offline: no credentials, no backend, no AWS call. Account IDs are placeholders.
#   terraform -chdir=infra/batch init -backend=false && terraform -chdir=infra/batch test

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "333333333333" }
  }
  mock_data "aws_organizations_organization" {
    defaults = { master_account_id = "111111111111" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { repository_url = "333333333333.dkr.ecr.ca-central-1.amazonaws.com/ecp-batch" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::333333333333:role/ecp-batch-mock" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:ca-central-1:333333333333:function:ecp-batch-stage" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:ca-central-1:333333333333:cluster/ecp-batch" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:ca-central-1:333333333333:task-definition/ecp-batch-pipeline:1" }
  }
  mock_resource "aws_sfn_state_machine" {
    defaults = { arn = "arn:aws:states:ca-central-1:333333333333:stateMachine:ecp-batch" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:ca-central-1:333333333333:ecp-batch-alerts" }
  }
}

# The tracked image.auto.tfvars (what is deployed) is loaded by `terraform test` too, so the
# stages start from their own inputs: no image, schedule off.
variables {
  expected_account_id = "333333333333"
  alert_email         = "alerts@example.invalid"
  image_digest        = ""
  schedule_enabled    = false
}

# --- stage 1: no image yet ------------------------------------------------------------------------

run "stage1_creates_nothing_that_runs_the_image" {
  command = plan

  assert {
    condition = (length(aws_lambda_function.stage) + length(aws_ecs_task_definition.pipeline)
      + length(aws_sfn_state_machine.batch) + length(aws_scheduler_schedule.daily)
    + length(aws_cloudwatch_metric_alarm.failures) + length(data.aws_ecr_image.batch)) == 0
    error_message = "Stage 1 (image_digest empty) must not create the function, task definition, state machine, schedule, alarm or image lookup."
  }
  assert {
    condition     = toset(keys(aws_iam_role.batch)) == toset(["stage", "task", "exec", "sfn", "scheduler"]) && toset(keys(aws_iam_role_policy.batch)) == toset(keys(aws_iam_role.batch))
    error_message = "Stage 1 creates all five roles and their policies, so stage 2 changes no IAM."
  }
  assert {
    condition     = alltrue([for k, r in aws_iam_role.batch : r.name == "ecp-batch-${k}" && r.permissions_boundary == "arn:aws:iam::333333333333:policy/ecp-workload-boundary"])
    error_message = "Every role is ecp-batch-<name> and carries the workload boundary (PLAN.md R2)."
  }
  assert {
    condition     = aws_iam_service_linked_role.ecs.aws_service_name == "ecs.amazonaws.com"
    error_message = "The ECS service-linked role is declared, not left to CreateCluster."
  }
  assert {
    condition     = aws_ecr_repository.batch.image_tag_mutability == "IMMUTABLE" && aws_ecr_repository.batch.image_scanning_configuration[0].scan_on_push
    error_message = "The repository has immutable tags and scans on push."
  }
  assert {
    condition     = aws_vpc_endpoint.s3.vpc_endpoint_type == "Gateway" && alltrue([for s in aws_subnet.public : !s.map_public_ip_on_launch])
    error_message = "S3 through a gateway endpoint; subnets do not hand out public IPs (the task asks for one)."
  }
  assert {
    condition     = aws_vpc_security_group_egress_rule.https.from_port == 443 && aws_vpc_security_group_egress_rule.https.to_port == 443 && aws_vpc_security_group_egress_rule.https.ip_protocol == "tcp"
    error_message = "The task's security group allows egress 443 only (no ingress: batch_plan_check.py checks the source, as ingress is computed)."
  }
  assert {
    condition     = aws_sns_topic_subscription.email.protocol == "email"
    error_message = "The alarm topic has one email subscription."
  }
}

run "stage1_refuses_another_account" {
  command = plan
  variables {
    expected_account_id = "444444444444"
  }
  expect_failures = [data.aws_caller_identity.current]
}

run "stage1_refuses_the_management_account" {
  command = plan
  override_data {
    target = data.aws_organizations_organization.this
    values = { master_account_id = "333333333333" }
  }
  expect_failures = [data.aws_organizations_organization.this]
}

run "a_tag_or_malformed_digest_is_refused" {
  command = plan
  variables {
    image_digest = "latest"
  }
  expect_failures = [var.image_digest]
}

run "an_uppercase_digest_is_refused" {
  command = plan
  variables {
    image_digest = "sha256:0123456789ABCDEF0123456789abcdef0123456789abcdef0123456789abcdef"
  }
  expect_failures = [var.image_digest]
}

run "another_region_is_refused" {
  command = plan
  variables {
    region = "us-east-1"
  }
  expect_failures = [var.region]
}

run "a_malformed_alert_email_is_refused" {
  command = plan
  variables {
    alert_email = "not an address"
  }
  expect_failures = [var.alert_email]
}

# --- stage 2: the pushed image, by digest ---------------------------------------------------------

run "stage2_runs_the_image_by_digest_with_the_schedule_disabled" {
  command = apply
  variables {
    image_digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }

  assert {
    condition     = aws_lambda_function.stage[0].image_uri == "333333333333.dkr.ecr.ca-central-1.amazonaws.com/ecp-batch@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    error_message = "The function runs the image by digest, never by tag."
  }
  assert {
    condition     = jsondecode(aws_ecs_task_definition.pipeline[0].container_definitions)[0].image == aws_lambda_function.stage[0].image_uri
    error_message = "Both steps run the same image digest."
  }
  assert {
    condition     = coalesce(aws_lambda_function.stage[0].reserved_concurrent_executions, -1) == -1
    error_message = "No reserved concurrency: the account's limit is 10, all of it unreserved (ADR-0022)."
  }
  assert {
    condition     = aws_lambda_function.stage[0].environment[0].variables["ECP_SOURCE"] == "synthetic" && contains(jsondecode(aws_ecs_task_definition.pipeline[0].container_definitions)[0].environment, { name = "ECP_SOURCE", value = "synthetic" })
    error_message = "Both steps run synthetic data only (ADR-0020)."
  }
  assert {
    condition     = aws_scheduler_schedule.daily[0].state == "DISABLED"
    error_message = "The schedule is created disabled (ADR-0022)."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.failures[0].alarm_actions == toset(["arn:aws:sns:ca-central-1:333333333333:ecp-batch-alerts"])
    error_message = "The alarm notifies the alerts topic."
  }
}

run "plan3_enables_the_schedule" {
  command = plan
  variables {
    image_digest     = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    schedule_enabled = true
  }

  assert {
    condition     = aws_scheduler_schedule.daily[0].state == "ENABLED"
    error_message = "schedule_enabled = true enables the schedule."
  }
}

# Plan 3 (ADR-0022): the universal target, so the execution gets a name Scheduler chooses. The input
# is written byte for byte: `jsonencode` would turn `<` and `>` into \u003c and \u003e and leave
# Scheduler no keyword to replace.
run "plan3_starts_the_state_machine_named_by_the_schedulers_execution_id" {
  command = apply
  variables {
    image_digest     = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    schedule_enabled = true
  }

  assert {
    condition     = aws_scheduler_schedule.daily[0].target[0].arn == "arn:aws:scheduler:::aws-sdk:sfn:startExecution"
    error_message = "The schedule uses the universal Step Functions StartExecution target."
  }
  assert {
    condition     = aws_scheduler_schedule.daily[0].target[0].input == "{\"StateMachineArn\":\"${aws_sfn_state_machine.batch[0].arn}\",\"Name\":\"<aws.scheduler.execution-id>\",\"Input\":\"{}\"}"
    error_message = "The target input is the exact reviewed bytes: the state machine, Name = Scheduler's execution ID, an empty input."
  }
  assert {
    condition     = !strcontains(aws_scheduler_schedule.daily[0].target[0].input, "u003")
    error_message = "The input must not be JSON-escaped."
  }
  assert {
    condition     = aws_scheduler_schedule.daily[0].target[0].role_arn == aws_iam_role.batch["scheduler"].arn
    error_message = "The schedule keeps its own role."
  }
}

# CI plans this stack with a placeholder alert address (ADR-0022): the subscription ignores endpoint
# changes, so a different address plans no replacement. Changing the address later is a deliberate
# replace step (taint, or remove the ignore).
run "a_different_alert_address_does_not_replace_the_subscription" {
  command = plan
  variables {
    image_digest     = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    schedule_enabled = true
    alert_email      = "ci@example.invalid"
  }

  assert {
    condition     = aws_sns_topic_subscription.email.endpoint == "alerts@example.invalid"
    error_message = "The subscription keeps its endpoint when alert_email changes (lifecycle ignore_changes)."
  }
}
