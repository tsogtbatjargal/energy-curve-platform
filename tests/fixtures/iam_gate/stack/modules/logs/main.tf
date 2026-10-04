variable "prefix" {
  type = string
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecp/${var.prefix}"
  retention_in_days = 7
}

output "group_arn" {
  value = aws_cloudwatch_log_group.this.arn
}
