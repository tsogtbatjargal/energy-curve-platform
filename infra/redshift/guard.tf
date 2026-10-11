# The stack runs only in the member account named by expected_account_id, never in the
# organization's management account (ADR-0021). A plan with the wrong profile stops here, before
# any resource is planned.
data "aws_caller_identity" "current" {
  lifecycle {
    postcondition {
      condition     = self.account_id == var.expected_account_id
      error_message = "The caller is not in expected_account_id."
    }
  }
}

data "aws_organizations_organization" "this" {
  lifecycle {
    postcondition {
      condition     = self.master_account_id != data.aws_caller_identity.current.account_id
      error_message = "This is the organization's management account; the Redshift stack runs in a member account."
    }
  }
}

# Redshift Serverless needs subnets in three Availability Zones. The account reports its own; the
# first three are used, and fewer than three stops the plan here.
data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}
