# ADR-0023: the warehouse's own network, with no way out. Three private subnets in three
# Availability Zones, a route table with only the local route, and a security group with no rules.
# No internet gateway, NAT gateway, endpoint or elastic IP: the Data API reaches the warehouse
# through the Redshift service, and COPY from S3 is proven in the first session (ADR-0023).
#
# Flow logs would cost more than the stack runs for; nothing can connect in or out.
#trivy:ignore:AWS-0178
resource "aws_vpc" "redshift" {
  cidr_block           = "10.43.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }

  lifecycle {
    precondition {
      condition     = length(local.zones) == 3
      error_message = "Redshift Serverless needs subnets in three Availability Zones; the account reports fewer."
    }
  }
}

# Takes over the VPC's default security group and removes its rules, so nothing can use it.
resource "aws_default_security_group" "redshift" {
  vpc_id = aws_vpc.redshift.id
}

resource "aws_subnet" "private" {
  for_each = local.subnets

  vpc_id                  = aws_vpc.redshift.id
  cidr_block              = each.value.cidr
  availability_zone       = each.value.az
  map_public_ip_on_launch = false

  tags = { Name = "${local.name}-${each.key}" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.redshift.id

  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  for_each = aws_subnet.private

  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

# No ingress and no egress rule: Terraform removes the allow-all egress AWS adds by default.
resource "aws_security_group" "redshift" {
  name        = local.name
  description = "The Redshift Serverless workgroup: no rules"
  vpc_id      = aws_vpc.redshift.id
}
