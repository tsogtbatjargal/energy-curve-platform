# ADR-0021: budgets and cost allocation tags belong to infra/org, in the management account, only.
# Only the management account can activate cost allocation tags, and a second budget would
# duplicate the alerts. Forgetting one (`removed`, destroy = false) is how bootstrap hands them over,
# so only live resources count. Without data.ecp.stack the rule applies, so it fails closed.
package terraform.ownership

import data.terraform.lib

org_only_types := {"aws_budgets_budget", "aws_ce_cost_allocation_tag"}

stack := s if {
	s := data.ecp.stack
} else := "MISSING"

deny contains msg if {
	stack != "org"
	some rc in lib.resources
	rc.type in org_only_types
	msg := sprintf("%s: only infra/org manages budgets and cost allocation tags (stack %s, ADR-0021)", [rc.address, stack])
}
