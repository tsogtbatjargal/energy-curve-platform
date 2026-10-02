variable "region" {
  type    = string
  default = "ca-central-1"
}

variable "owner" {
  type    = string
  default = "tsogtbatjargal"
}

variable "github_repo" {
  description = "owner/name of the repository allowed to assume the CI roles"
  type        = string
  default     = "tsogtbatjargal/energy-curve-platform"
}

variable "budget_limit_usd" {
  type    = number
  default = 40
}

variable "budget_alert_emails" {
  type = list(string)

  validation {
    condition     = length(var.budget_alert_emails) > 0
    error_message = "At least one alert email is required."
  }
}
