# Owned by this stack since ADR-0021 phase 1a, imported from the management bootstrap. Only the
# management account can activate cost allocation tags, and destroying this resource would set the
# tag Inactive and blind the budget's tag filter, so it is never destroyed or re-created.
import {
  to = aws_ce_cost_allocation_tag.project
  id = "project"
}

import {
  to = aws_budgets_budget.project
  id = "${local.account_id}:energy-curve-platform"
}

# Cost allocation tags must be active before a tag-filtered budget sees spend (up to 24 h).
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

  # ADR-0021 phase 1c: everything in the workload account, or anything tagged with the project
  # (its management-account resources). The OR needs filter_expression, which replaces the
  # deprecated cost_filter and requires metrics in place of cost_types; UnblendedCost is what the
  # default cost types measured.
  metrics = ["UnblendedCost"]

  filter_expression {
    or {
      dimensions {
        key    = "LINKED_ACCOUNT"
        values = [aws_organizations_account.workloads.id]
      }
    }
    or {
      tags {
        key    = "user:project" # user-defined cost allocation tags take the user: prefix
        values = ["energy-curve-platform"]
      }
    }
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
