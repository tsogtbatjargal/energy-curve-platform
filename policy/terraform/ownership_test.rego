package terraform.ownership_test

import data.terraform.ownership

rc(type, actions) := {"address": sprintf("%s.project", [type]), "mode": "managed", "type": type, "change": {"actions": actions, "after": {}, "after_unknown": {}}}

budget(actions) := rc("aws_budgets_budget", actions)

tag(actions) := rc("aws_ce_cost_allocation_tag", actions)

test_budget_and_tag_denied_outside_org if {
	count(ownership.deny) == 2 with input as {"resource_changes": [budget(["create"]), tag(["no-op"])]}
		with data.ecp.stack as "bootstrap"
}

test_denied_in_a_workload_stack if {
	count(ownership.deny) == 1 with input as {"resource_changes": [budget(["update"])]}
		with data.ecp.stack as "batch"
}

test_missing_stack_fails_closed if {
	count(ownership.deny) == 1 with input as {"resource_changes": [tag(["create"])]}
}

test_allowed_in_org if {
	count(ownership.deny) == 0 with input as {"resource_changes": [budget(["no-op"]), tag(["no-op"])]}
		with data.ecp.stack as "org"
}

test_forgetting_hands_them_over if {
	count(ownership.deny) == 0 with input as {"resource_changes": [budget(["forget"]), tag(["forget"])]}
		with data.ecp.stack as "bootstrap"
}

test_a_replacement_is_live if {
	count(ownership.deny) == 1 with input as {"resource_changes": [budget(["delete", "create"])]}
		with data.ecp.stack as "bootstrap"
}
