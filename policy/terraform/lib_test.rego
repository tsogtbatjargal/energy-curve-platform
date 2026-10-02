package terraform.lib_test

import data.terraform.lib

rc(addr, actions) := {"address": addr, "mode": "managed", "type": "aws_s3_bucket", "name": addr, "change": {"actions": actions, "after": {}}}

plan := {"resource_changes": [
	rc("unchanged", ["no-op"]),
	rc("updated", ["update"]),
	rc("created", ["create"]),
	rc("replaced_destroy_first", ["delete", "create"]),
	rc("replaced_create_first", ["create", "delete"]),
	rc("deleted", ["delete"]),
	rc("forgotten", ["forget"]),
	{"address": "data.x", "mode": "data", "type": "aws_s3_bucket", "name": "x", "change": {"actions": ["read"]}},
]}

test_live_set_includes_both_replacement_orders_and_unchanged if {
	live := {r.address | some r in lib.resources} with input as plan
	live == {"unchanged", "updated", "created", "replaced_destroy_first", "replaced_create_first"}
}

test_unknown_future_action_fails_closed if {
	live := {r.address | some r in lib.resources} with input as {"resource_changes": [rc("future", ["transmogrify"])]}
	live == {"future"}
}

test_module_address_normalised if {
	lib.module_address({"module_address": null}) == ""
	lib.module_address({}) == ""
	lib.module_address({"module_address": "module.a"}) == "module.a"
}
