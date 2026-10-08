# Stage 2: stage, then run the pipeline task, then check its exit code (ADR-0022). The execution
# name is the run_id of both steps (ADR-0020). Exit 3 (quarantined) or any other non-zero exit
# fails the execution, so the alarm fires; the Choice does not rely on how runTask.sync treats a
# container's exit code, which the integration page does not state.
resource "aws_sfn_state_machine" "batch" {
  count = local.stage2 ? 1 : 0

  name     = local.name
  type     = "STANDARD"
  role_arn = aws_iam_role.batch["sfn"].arn

  definition = jsonencode({
    Comment        = "M4 daily batch: stage, then pipeline (ADR-0020, ADR-0022)"
    StartAt        = "Stage"
    TimeoutSeconds = 900
    States = {
      Stage = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.stage[0].arn
          Payload      = { "run_id.$" = "$$.Execution.Name" }
        }
        ResultPath = null
        Retry = [{
          ErrorEquals     = ["States.ALL"]
          IntervalSeconds = 5
          MaxAttempts     = 2
          BackoffRate     = 2
          JitterStrategy  = "FULL"
        }]
        Next = "Pipeline"
      }
      Pipeline = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          Cluster        = aws_ecs_cluster.batch.arn
          TaskDefinition = aws_ecs_task_definition.pipeline[0].arn
          LaunchType     = "FARGATE"
          NetworkConfiguration = {
            AwsvpcConfiguration = {
              Subnets        = [for s in aws_subnet.public : s.id]
              SecurityGroups = [aws_security_group.task.id]
              AssignPublicIp = "ENABLED"
            }
          }
          Overrides = {
            ContainerOverrides = [{
              Name        = local.container_name
              "Command.$" = "States.Array('task', $$.Execution.Name)"
            }]
          }
        }
        ResultSelector = { "exit_code.$" = "$.Containers[0].ExitCode" }
        ResultPath     = "$.pipeline"
        Next           = "ExitCode"
      }
      ExitCode = {
        Type    = "Choice"
        Choices = [{ Variable = "$.pipeline.exit_code", NumericEquals = 0, Next = "Done" }]
        Default = "PipelineFailed"
      }
      Done = { Type = "Succeed" }
      PipelineFailed = {
        Type  = "Fail"
        Error = "PipelineFailed"
        Cause = "The pipeline task exited non-zero (3 = quarantined)."
      }
    }
  })
}

# Created disabled; plan 3 enables it after the first manual run and its replay pass (ADR-0022).
resource "aws_scheduler_schedule" "daily" {
  count = local.stage2 ? 1 : 0

  name                         = "${local.name}-daily"
  schedule_expression          = "cron(0 12 * * ? *)"
  schedule_expression_timezone = "America/Toronto"
  state                        = var.schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_sfn_state_machine.batch[0].arn
    role_arn = aws_iam_role.batch["scheduler"].arn
    input    = jsonencode({})

    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}
