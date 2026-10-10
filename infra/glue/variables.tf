variable "region" {
  type    = string
  default = "ca-central-1"

  validation {
    condition     = var.region == "ca-central-1"
    error_message = "The Glue stack runs in ca-central-1 only."
  }
}

variable "owner" {
  type    = string
  default = "tsogtbatjargal"
}

# The member account (ecp-workloads, ADR-0021). The stack refuses to plan anywhere else
# (guard.tf). CI derives it from the plan role; locally it comes from the git-ignored tfvars.
variable "expected_account_id" {
  description = "The 12-digit account this stack must run in."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "expected_account_id must be a 12-digit AWS account ID."
  }
}
