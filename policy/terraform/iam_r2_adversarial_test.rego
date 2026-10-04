# Adversarial black-box tests for PLAN.md R2 (F-9): workload roles must carry the
# ecp-workload-boundary permissions boundary. Written against the frozen requirements only.
package terraform.iam_r2_adversarial_test

import data.terraform.iam

good := "arn:aws:iam::123456789012:policy/ecp-workload-boundary"

r2_phrase := "workload roles need permissions_boundary"

r2_msgs contains msg if {
	some msg in iam.deny
	contains(msg, r2_phrase)
}

role_rc(addr, actions, after, unknown) := {
	"address": addr,
	"mode": "managed",
	"type": "aws_iam_role",
	"name": "task",
	"change": {"actions": actions, "before": null, "after": after, "after_unknown": unknown},
}

role_with(b) := role_rc("aws_iam_role.task", ["create"], {"name": "ecp-task", "permissions_boundary": b}, {})

in_module(rc, module, index) := object.union(rc, {"module_address": module, "index": index})

plan(rcs) := {"resource_changes": rcs}

# --- the valid boundary passes (positive controls) ---

test_good_boundary_allowed_in_workload_stack if {
	count(r2_msgs) == 0 with input as plan([role_with(good)]) with data.ecp.stack as "batch"
}

test_good_boundary_allowed_with_no_stack if {
	count(r2_msgs) == 0 with input as plan([role_with(good)])
}

test_good_boundary_allowed_in_module_with_keys if {
	rcs := [
		in_module(role_rc("module.batch.aws_iam_role.task[0]", ["create"], {"permissions_boundary": good}, {}), "module.batch", 0),
		in_module(role_rc("module.batch.aws_iam_role.task[1]", ["create"], {"permissions_boundary": good}, {}), "module.batch", 1),
		in_module(role_rc("module.a.module.b.aws_iam_role.task[\"etl\"]", ["update"], {"permissions_boundary": good}, {}), "module.a.module.b", "etl"),
	]
	count(r2_msgs) == 0 with input as plan(rcs) with data.ecp.stack as "batch"
}

# --- missing, null, empty, unknown ---

test_missing_boundary_key_denied if {
	rc := role_rc("aws_iam_role.task", ["create"], {"name": "ecp-task"}, {})
	count(r2_msgs) == 1 with input as plan([rc]) with data.ecp.stack as "batch"
}

test_null_and_empty_boundary_denied if {
	every b in [null, ""] {
		count(r2_msgs) == 1 with input as plan([role_with(b)]) with data.ecp.stack as "batch"
	}
}

test_non_string_boundary_denied if {
	every b in [[good], {"arn": good}, 0, true] {
		count(r2_msgs) == 1 with input as plan([role_with(b)]) with data.ecp.stack as "batch"
	}
}

test_unknown_boundary_denied if {
	absent := role_rc("aws_iam_role.task", ["create"], {"name": "ecp-task"}, {"permissions_boundary": true})
	nulled := role_rc("aws_iam_role.task", ["create"], {"name": "ecp-task", "permissions_boundary": null}, {"permissions_boundary": true})
	count(r2_msgs) == 1 with input as plan([absent]) with data.ecp.stack as "batch"
	count(r2_msgs) == 1 with input as plan([nulled]) with data.ecp.stack as "batch"
}

test_after_null_role_denied if {
	# A live role whose `after` is null carries no boundary at all.
	rc := role_rc("aws_iam_role.task", ["create"], null, {})
	count(r2_msgs) == 1 with input as plan([rc]) with data.ecp.stack as "batch"
}

# --- lookalikes ---

test_lookalike_names_denied if {
	every b in [
		"arn:aws:iam::123456789012:policy/ecp-workload-boundary-old",
		"arn:aws:iam::123456789012:policy/ecp-workload-boundar",
		"arn:aws:iam::123456789012:policy/ECP-Workload-Boundary",
		"arn:aws:iam::123456789012:policy/team/ecp-workload-boundary",
		"arn:aws:iam::123456789012:policy/ecp-workload-boundary/",
		"arn:aws:iam::123456789012:role/ecp-workload-boundary",
		"arn:aws:iam::123456789012:policy/old-ecp-workload-boundary",
		"arn:aws:iam::aws:policy/AdministratorAccess",
	] {
		count(r2_msgs) == 1 with input as plan([role_with(b)]) with data.ecp.stack as "batch"
	}
}

test_whitespace_lookalikes_denied if {
	every b in [
		sprintf("%s ", [good]),
		sprintf(" %s", [good]),
		sprintf("%s\n", [good]),
		sprintf("%s\t", [good]),
	] {
		count(r2_msgs) == 1 with input as plan([role_with(b)]) with data.ecp.stack as "batch"
	}
}

test_bad_account_format_denied if {
	every b in [
		"ecp-workload-boundary",
		":policy/ecp-workload-boundary",
		"arn:aws:iam::aws:policy/ecp-workload-boundary",
		"arn:aws:iam::*:policy/ecp-workload-boundary",
		"arn:aws:iam::12345:policy/ecp-workload-boundary",
		"arn:aws:iam::1234567890123:policy/ecp-workload-boundary",
		"arn:aws:iam:ca-central-1:123456789012:policy/ecp-workload-boundary",
		"arn:aws:s3::123456789012:policy/ecp-workload-boundary",
	] {
		count(r2_msgs) == 1 with input as plan([role_with(b)]) with data.ecp.stack as "batch"
	}
}

test_other_partition_denied if {
	every b in [
		"arn:aws-us-gov:iam::123456789012:policy/ecp-workload-boundary",
		"arn:aws-cn:iam::123456789012:policy/ecp-workload-boundary",
		"arnx:aws:iam::123456789012:policy/ecp-workload-boundary",
	] {
		count(r2_msgs) == 1 with input as plan([role_with(b)]) with data.ecp.stack as "batch"
	}
}

# --- modules and index keys: every instance is checked and reported on its own ---

test_bad_roles_in_modules_each_reported if {
	rcs := [
		in_module(role_rc("module.batch.aws_iam_role.task[0]", ["create"], {"permissions_boundary": null}, {}), "module.batch", 0),
		in_module(role_rc("module.batch.aws_iam_role.task[1]", ["create"], {}, {}), "module.batch", 1),
		in_module(role_rc("module.batch.aws_iam_role.task[\"etl\"]", ["create"], {"permissions_boundary": ""}, {}), "module.batch", "etl"),
		in_module(role_rc("module.a.module.b.aws_iam_role.task[\"load\"]", ["create"], {"permissions_boundary": "arn:aws:iam::123456789012:policy/ecp-workload-boundary-old"}, {}), "module.a.module.b", "load"),
		in_module(role_rc("module.batch.aws_iam_role.task[2]", ["create"], {"permissions_boundary": good}, {}), "module.batch", 2),
	]
	count(r2_msgs) == 4 with input as plan(rcs) with data.ecp.stack as "batch"
}

test_root_and_module_role_same_name_both_reported if {
	rcs := [
		role_rc("aws_iam_role.task", ["create"], {}, {}),
		in_module(role_rc("module.m.aws_iam_role.task", ["create"], {}, {}), "module.m", null),
	]
	count(r2_msgs) == 2 with input as plan(rcs) with data.ecp.stack as "batch"
}

# --- replacements are checked; deletions and forgets are skipped ---

test_replacement_with_bad_boundary_denied_both_orders if {
	every order in [["delete", "create"], ["create", "delete"]] {
		rc := role_rc("aws_iam_role.task", order, {"permissions_boundary": null}, {})
		count(r2_msgs) == 1 with input as plan([rc]) with data.ecp.stack as "batch"
	}
}

test_replacement_with_unknown_boundary_denied_both_orders if {
	every order in [["delete", "create"], ["create", "delete"]] {
		rc := role_rc("aws_iam_role.task", order, {}, {"permissions_boundary": true})
		count(r2_msgs) == 1 with input as plan([rc]) with data.ecp.stack as "batch"
	}
}

test_replacement_with_good_boundary_allowed_both_orders if {
	every order in [["delete", "create"], ["create", "delete"]] {
		rc := role_rc("aws_iam_role.task", order, {"permissions_boundary": good}, {})
		count(r2_msgs) == 0 with input as plan([rc]) with data.ecp.stack as "batch"
	}
}

test_update_and_noop_without_boundary_denied if {
	every acts in [["update"], ["no-op"], ["read"]] {
		rc := role_rc("aws_iam_role.task", acts, {"permissions_boundary": null}, {})
		count(r2_msgs) == 1 with input as plan([rc]) with data.ecp.stack as "batch"
	}
}

test_update_clearing_boundary_denied if {
	rc := {
		"address": "aws_iam_role.task",
		"mode": "managed",
		"type": "aws_iam_role",
		"name": "task",
		"change": {"actions": ["update"], "before": {"permissions_boundary": good}, "after": {"permissions_boundary": null}, "after_unknown": {}},
	}
	count(r2_msgs) == 1 with input as plan([rc]) with data.ecp.stack as "batch"
}

test_unrecognised_action_fails_closed if {
	rc := role_rc("aws_iam_role.task", ["future-action"], {"permissions_boundary": null}, {})
	count(r2_msgs) == 1 with input as plan([rc]) with data.ecp.stack as "batch"
}

test_deleted_and_forgotten_roles_skipped if {
	every acts in [["delete"], ["forget"]] {
		rc := role_rc("aws_iam_role.task", acts, null, {})
		count(r2_msgs) == 0 with input as plan([rc]) with data.ecp.stack as "batch"
	}
}

test_deleted_mixed_with_live_roles if {
	gone := role_rc("aws_iam_role.old", ["delete"], null, {})
	forgot := role_rc("aws_iam_role.forgot", ["forget"], {"permissions_boundary": null}, {})
	ok := role_rc("aws_iam_role.ok", ["create"], {"permissions_boundary": good}, {})
	bad := role_rc("aws_iam_role.bad", ["create"], {"permissions_boundary": ""}, {})
	count(r2_msgs) == 0 with input as plan([gone, forgot, ok]) with data.ecp.stack as "batch"
	count(r2_msgs) == 1 with input as plan([gone, forgot, ok, bad]) with data.ecp.stack as "batch"
}

test_data_source_role_ignored if {
	rc := {"address": "data.aws_iam_role.x", "mode": "data", "type": "aws_iam_role", "name": "x", "change": {"actions": ["read"], "after": {}, "after_unknown": {}}}
	count(r2_msgs) == 0 with input as plan([rc]) with data.ecp.stack as "batch"
}

# --- stacks: only exactly "bootstrap" is exempt ---

test_workload_stacks_denied if {
	every s in ["batch", "demo", "glue", "Bootstrap", "BOOTSTRAP", "", " bootstrap", "bootstrap ", "bootstrap-old", "infra/bootstrap"] {
		count(r2_msgs) == 1 with input as plan([role_with(null)]) with data.ecp.stack as s
	}
}

test_missing_stack_is_workload if {
	count(r2_msgs) == 1 with input as plan([role_with(null)])
	count(r2_msgs) == 1 with input as plan([role_rc("aws_iam_role.task", ["create"], {}, {"permissions_boundary": true})])
}

test_non_string_stack_is_workload if {
	every s in [null, ["bootstrap"], {"bootstrap": true}, 0] {
		count(r2_msgs) == 1 with input as plan([role_with(null)]) with data.ecp.stack as s
	}
}

test_bootstrap_stack_exempt if {
	rcs := [
		role_with(null),
		role_rc("aws_iam_role.gha", ["create"], {"name": "ecp-gha-deploy"}, {}),
		in_module(role_rc("module.m.aws_iam_role.x[0]", ["delete", "create"], {}, {"permissions_boundary": true}), "module.m", 0),
	]
	count(r2_msgs) == 0 with input as plan(rcs) with data.ecp.stack as "bootstrap"
}

test_bootstrap_exemption_does_not_disable_other_iam_rules if {
	a := {"address": "aws_iam_role_policy_attachment.x", "mode": "managed", "type": "aws_iam_role_policy_attachment", "change": {"actions": ["create"], "after": {"policy_arn": "arn:aws:iam::aws:policy/AdministratorAccess"}, "after_unknown": {}}}
	count(iam.deny) == 1 with input as plan([a]) with data.ecp.stack as "bootstrap"
}

test_r2_does_not_flag_compliant_plan_alongside_existing_rules if {
	ok := role_with(good)
	att := {"address": "aws_iam_role_policy_attachment.x", "mode": "managed", "type": "aws_iam_role_policy_attachment", "change": {"actions": ["create"], "after": {"policy_arn": "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"}, "after_unknown": {}}}
	count(iam.deny) == 0 with input as plan([ok, att]) with data.ecp.stack as "batch"
}
