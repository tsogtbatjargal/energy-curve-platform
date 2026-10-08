# Fargate only; Container Insights off (a cost, and the alarm watches the state machine instead).
resource "aws_ecs_cluster" "batch" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  depends_on = [aws_iam_service_linked_role.ecs]
}

resource "aws_ecs_cluster_capacity_providers" "batch" {
  cluster_name       = aws_ecs_cluster.batch.name
  capacity_providers = ["FARGATE"]
}

# Stage 2. 0.5 vCPU and 1 GB: the smallest Fargate size for 0.5 vCPU, over 10 times the measured
# peak (ADR-0022). The command comes from the state machine: `task <run_id>`.
resource "aws_ecs_task_definition" "pipeline" {
  count = local.stage2 ? 1 : 0

  family                   = local.task_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.batch["exec"].arn
  task_role_arn            = aws_iam_role.batch["task"].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name       = local.container_name
    image      = local.image_uri
    essential  = true
    entryPoint = ["python", "-m", "energy_curves.cloud"]
    environment = [
      { name = "ECP_SOURCE", value = "synthetic" },
      { name = "ECP_STORE_URL", value = local.store_url },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.pipeline.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = local.container_name
      }
    }
  }])
}
