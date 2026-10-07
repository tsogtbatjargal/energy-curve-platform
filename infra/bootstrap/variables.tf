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

# ADR-0021 phase 3: this stack runs as two instances. Each sets expected_account_id in its own
# git-ignored tfvars, and refuses to plan in any other account (guard.tf). CI derives it from the
# plan role it assumes.
variable "expected_account_id" {
  description = "The 12-digit account this instance must run in."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "expected_account_id must be a 12-digit AWS account ID."
  }
}

# false (the default) is the management instance, exactly as before: the state bucket is owned by
# infra/org and the GitHub OIDC provider by another project. true is the member instance in
# ecp-workloads: it creates its own state bucket and OIDC provider, and imports and restricts
# OrganizationAccountAccessRole (the break-glass role).
variable "member_instance" {
  description = "true only for the ecp-workloads instance (ADR-0021 phase 3)."
  type        = bool
  default     = false
}
