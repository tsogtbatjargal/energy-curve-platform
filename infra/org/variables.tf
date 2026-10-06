variable "region" {
  type    = string
  default = "ca-central-1"
}

variable "owner" {
  type    = string
  default = "tsogtbatjargal"
}

variable "budget_limit_usd" {
  type    = number
  default = 40
}

variable "budget_alert_emails" {
  type      = list(string)
  sensitive = true # keeps the address out of plan output

  validation {
    condition     = length(var.budget_alert_emails) > 0
    error_message = "At least one alert email is required."
  }
}

# The workload account's root email (ADR-0021 phase 1b). Never committed: terraform.tfvars only.
variable "workload_account_email" {
  type      = string
  sensitive = true

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.workload_account_email))
    error_message = "workload_account_email must be an email address."
  }
}

# The Identity Center user given access to the workload account (ADR-0021 phase 1c), by UserName.
# Never committed: terraform.tfvars only.
variable "identity_center_user_name" {
  type      = string
  sensitive = true

  validation {
    condition     = length(trimspace(var.identity_center_user_name)) > 0
    error_message = "identity_center_user_name must be the Identity Center UserName."
  }
}
