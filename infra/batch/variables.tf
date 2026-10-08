variable "region" {
  type    = string
  default = "ca-central-1"

  validation {
    condition     = var.region == "ca-central-1"
    error_message = "The batch stack runs in ca-central-1 only."
  }
}

variable "owner" {
  type    = string
  default = "tsogtbatjargal"
}

# The member account (ecp-workloads, ADR-0021), set in the git-ignored terraform.tfvars. The stack
# refuses to plan anywhere else (guard.tf).
variable "expected_account_id" {
  description = "The 12-digit account this stack must run in."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "expected_account_id must be a 12-digit AWS account ID."
  }
}

# ADR-0022: empty is stage 1 (nothing that runs the image); the pushed image's digest is stage 2.
# A digest, never a tag, so the plan names exactly the image that was scanned and approved.
variable "image_digest" {
  description = "\"\" for stage 1, or the pushed image's sha256:<64 hex> digest for stage 2."
  type        = string
  default     = ""

  validation {
    condition     = var.image_digest == "" || can(regex("^sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "image_digest must be \"\" or sha256: followed by 64 lowercase hex digits."
  }
}

# Set only in the git-ignored terraform.tfvars: the address is never in the repo. Not sensitive (an
# address is no secret, and a sensitive value would hide the subscription from the plan review).
variable "alert_email" {
  description = "Where the failure alarm's email goes. The subscription waits for its confirmation."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s\"]+@[^@\\s\"]+\\.[^@\\s\"]+$", var.alert_email))
    error_message = "alert_email must be one email address."
  }
}

# Plan 3 (ADR-0022): enabled only after the first manual run and its replay pass.
variable "schedule_enabled" {
  type    = bool
  default = false
}
