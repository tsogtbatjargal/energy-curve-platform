# Owned by this stack since ADR-0021 phase 1a, imported from the management bootstrap. Only the
# management account can activate cost allocation tags, and destroying this resource would set the
# tag Inactive (the budget filtered on it until ADR-0021 phase 1c; cost reports still group by it),
# so it is never destroyed or re-created.
import {
  to = aws_ce_cost_allocation_tag.project
  id = "project"
}

import {
  to = aws_budgets_budget.project
  id = "${local.account_id}:energy-curve-platform"
}

# Cost allocation tags take up to 24 h to become active for cost data.
resource "aws_ce_cost_allocation_tag" "project" {
  tag_key = "project"
  status  = "Active"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_budgets_budget" "project" {
  name         = "energy-curve-platform"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  tags         = local.imported_tags

  # ADR-0021 phase 1c (user, 2026-10-06): everything in the workload account. The legacy
  # cost_filter is changed in place; filter_expression could express "or the project tag", but
  # the provider keeps the Optional+Computed cost_filter and cost_types next to it, and the
  # Budgets API refuses both filter styles together. Tagged costs left in the management account
  # (the state bucket) are no longer counted.
  cost_filter {
    name   = "LinkedAccount"
    values = [aws_organizations_account.workloads.id]
  }

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = var.budget_alert_emails
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.budget_alert_emails
  }

  depends_on = [aws_ce_cost_allocation_tag.project]

  lifecycle {
    prevent_destroy = true
  }
}
