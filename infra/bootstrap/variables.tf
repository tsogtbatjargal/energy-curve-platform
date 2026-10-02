variable "region" {
  type    = string
  default = "ca-central-1"
}

variable "owner" {
  type    = string
  default = "tsogtbatjargal"
}

variable "github_oidc_subject_prefix" {
  description = <<-EOT
    OIDC `sub` prefix of the repository allowed to assume the CI roles. This repo uses GitHub's
    immutable subject format (owner@owner_id/repo@repo_id), which survives renames and cannot be
    claimed by a recreated repo of the same name. Check with:
    gh api repos/<owner>/<repo>/actions/oidc/customization/sub
  EOT
  type        = string
  default     = "repo:tsogtbatjargal@122837521/energy-curve-platform@1402272172"
}

variable "budget_limit_usd" {
  type    = number
  default = 40
}

variable "budget_alert_emails" {
  type      = list(string)
  sensitive = true # keeps the address out of public CI plan logs

  validation {
    condition     = length(var.budget_alert_emails) > 0
    error_message = "At least one alert email is required."
  }
}
