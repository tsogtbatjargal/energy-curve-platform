# ADR-0021 phase 1a: infra/org imports these and owns them from then on. This stack forgets them
# without destroying anything: the state bucket holds every stack's state, and destroying the cost
# allocation tag would set it Inactive. The budget and the tag leave this stack's code for good, so
# no instance of it (the member account's included) creates a budget or a cost allocation tag.
# Apply infra/org's imports first, then this plan.

removed {
  from = aws_s3_bucket.tfstate
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_s3_bucket_versioning.tfstate
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_s3_bucket_server_side_encryption_configuration.tfstate
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_s3_bucket_public_access_block.tfstate
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_s3_bucket_ownership_controls.tfstate
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_s3_bucket_lifecycle_configuration.tfstate
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_s3_bucket_policy.tfstate
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_budgets_budget.project
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_ce_cost_allocation_tag.project
  lifecycle {
    destroy = false
  }
}
