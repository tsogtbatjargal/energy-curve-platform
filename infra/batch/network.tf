# ADR-0004 batch profile: public subnets, a public IP for the task, egress 443 only, no ingress.
# No NAT gateway and no interface endpoints; S3 goes through a free gateway endpoint.
locals {
  subnets = {
    a = { cidr = "10.42.0.0/24", az = "${var.region}a" }
    b = { cidr = "10.42.1.0/24", az = "${var.region}b" }
  }
}

# Flow logs would cost more than the stack runs for; nothing listens in this VPC (no ingress).
#trivy:ignore:AWS-0178
resource "aws_vpc" "batch" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

# Takes over the VPC's default security group and removes its rules, so nothing can use it.
resource "aws_default_security_group" "batch" {
  vpc_id = aws_vpc.batch.id
}

resource "aws_internet_gateway" "batch" {
  vpc_id = aws_vpc.batch.id
}

resource "aws_subnet" "public" {
  for_each = local.subnets

  vpc_id                  = aws_vpc.batch.id
  cidr_block              = each.value.cidr
  availability_zone       = each.value.az
  map_public_ip_on_launch = false # the task asks for its own public IP (AssignPublicIp)

  tags = { Name = "${local.name}-${each.key}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.batch.id
}

resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.batch.id
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.batch.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
}

resource "aws_security_group" "task" {
  name        = "ecp-batch-task"
  description = "The pipeline task: no ingress, HTTPS egress only"
  vpc_id      = aws_vpc.batch.id
}

# ECR, CloudWatch Logs and STS have no fixed address ranges, and there is no NAT or interface
# endpoint to pin them to, so HTTPS egress is open; nothing can connect in.
#trivy:ignore:AWS-0104
resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.task.id
  description       = "HTTPS to AWS APIs (ECR, S3, CloudWatch Logs)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
