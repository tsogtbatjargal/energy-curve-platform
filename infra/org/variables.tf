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

# Used from phase 1b, when the member account is created (ADR-0021); unused in phase 1a.
variable "workload_account_email" {
  type      = string
  default   = null
  sensitive = true
}
