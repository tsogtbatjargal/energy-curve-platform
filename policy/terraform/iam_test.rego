package terraform.iam_test

import data.terraform.iam

rc(type, after) := {"address": sprintf("%s.x", [type]), "mode": "managed", "type": type, "change": {"actions": ["create"], "after": after, "after_unknown": {}}}

policy_doc(stmts) := json.marshal({"Version": "2012-10-17", "Statement": stmts})

test_star_on_star_denied if {
	p := rc("aws_iam_role_policy", {"policy": policy_doc([{"Effect": "Allow", "Action": "*", "Resource": "*"}])})
	count(iam.deny) == 1 with input as {"resource_changes": [p]}
}

test_star_in_action_list_denied if {
	p := rc("aws_iam_policy", {"policy": policy_doc([{"Effect": "Allow", "Action": ["s3:GetObject", "*"], "Resource": ["*"]}])})
	count(iam.deny) == 1 with input as {"resource_changes": [p]}
}

test_scoped_wildcard_allowed if {
	p := rc("aws_iam_role_policy", {"policy": policy_doc([{"Effect": "Allow", "Action": "iam:*Role*", "Resource": "arn:aws:iam::123:role/ecp-*"}])})
	count(iam.deny) == 0 with input as {"resource_changes": [p]}
}

test_deny_star_statement_allowed if {
	p := rc("aws_iam_role_policy", {"policy": policy_doc([{"Effect": "Deny", "Action": "*", "Resource": "*"}])})
	count(iam.deny) == 0 with input as {"resource_changes": [p]}
}

test_admin_attachment_denied if {
	a := rc("aws_iam_role_policy_attachment", {"policy_arn": "arn:aws:iam::aws:policy/AdministratorAccess"})
	count(iam.deny) == 1 with input as {"resource_changes": [a]}
}

test_unknown_policy_warns if {
	p := {"address": "aws_iam_role_policy.x", "mode": "managed", "type": "aws_iam_role_policy", "change": {"actions": ["create"], "after": {}, "after_unknown": {"policy": true}}}
	count(iam.warn) == 1 with input as {"resource_changes": [p]}
}

test_replaced_admin_attachment_denied_both_orders if {
	ok := {"address": "aws_iam_role_policy_attachment.ok", "mode": "managed", "type": "aws_iam_role_policy_attachment", "change": {"actions": ["no-op"], "after": {"policy_arn": "arn:aws:iam::aws:policy/ReadOnlyAccess"}, "after_unknown": {}}}
	every order in [["delete", "create"], ["create", "delete"]] {
		bad := {"address": "aws_iam_role_policy_attachment.bad", "mode": "managed", "type": "aws_iam_role_policy_attachment", "change": {"actions": order, "after": {"policy_arn": "arn:aws:iam::aws:policy/AdministratorAccess"}, "after_unknown": {}}}
		iam.deny == {"aws_iam_role_policy_attachment.bad: AdministratorAccess attachment is not allowed"} with input as {"resource_changes": [ok, bad]}
	}
}

# --- PLAN.md R2: the workload permissions boundary ---

boundary := "arn:aws:iam::123456789012:policy/ecp-workload-boundary"

role(after, unknown) := {"address": "module.app.aws_iam_role.task", "mode": "managed", "type": "aws_iam_role", "change": {"actions": ["create"], "after": after, "after_unknown": unknown}}

test_workload_role_with_the_boundary_allowed if {
	count(iam.deny) == 0 with input as {"resource_changes": [role({"permissions_boundary": boundary}, {})]}
		with data.ecp.stack as "batch"
}

test_workload_role_without_a_boundary_denied if {
	every after in [{}, {"permissions_boundary": null}, {"permissions_boundary": ""}] {
		count(iam.deny) == 1 with input as {"resource_changes": [role(after, {})]}
			with data.ecp.stack as "batch"
	}
}

test_workload_role_with_another_boundary_denied if {
	every b in [
		"arn:aws:iam::123456789012:policy/ecp-workload-boundary-v2",
		"arn:aws:iam::123456789012:policy/other",
		"arn:aws:iam::aws:policy/AdministratorAccess",
	] {
		count(iam.deny) == 1 with input as {"resource_changes": [role({"permissions_boundary": b}, {})]}
			with data.ecp.stack as "demo"
	}
}

test_workload_role_with_an_unknown_boundary_denied if {
	count(iam.deny) == 1 with input as {"resource_changes": [role({}, {"permissions_boundary": true})]}
		with data.ecp.stack as "batch"
}

test_bootstrap_roles_are_exempt if {
	count(iam.deny) == 0 with input as {"resource_changes": [role({}, {})]}
		with data.ecp.stack as "bootstrap"
}

test_a_missing_stack_name_counts_as_a_workload_stack if {
	iam.deny == {"module.app.aws_iam_role.task: workload roles need permissions_boundary = the ecp-workload-boundary policy ARN, known at plan time (stack MISSING, PLAN.md R2)"} with input as {"resource_changes": [role({}, {})]}
}

test_deleted_roles_are_not_checked if {
	gone := json.patch(role({}, {}), [{"op": "replace", "path": "/change/actions", "value": ["delete"]}])
	count(iam.deny) == 0 with input as {"resource_changes": [gone]}
		with data.ecp.stack as "batch"
}

# PLAN.md R2 for the Redshift stack: both roles carry the boundary, and a trust never names everyone.
redshift_role(boundary, trust) := rc("aws_iam_role", {"permissions_boundary": boundary, "assume_role_policy": json.marshal({"Version": "2012-10-17", "Statement": trust})})

test_redshift_role_without_boundary_denied if {
	r := redshift_role(null, [{"Effect": "Allow", "Principal": {"Service": "redshift.amazonaws.com"}, "Action": "sts:AssumeRole"}])
	count(iam.deny) == 1 with input as {"resource_changes": [r]} with data.ecp.stack as "redshift"
}

test_redshift_role_with_boundary_and_service_trust_allowed if {
	r := redshift_role("arn:aws:iam::123456789012:policy/ecp-workload-boundary", [{"Effect": "Allow", "Principal": {"Service": ["redshift.amazonaws.com", "redshift-serverless.amazonaws.com"]}, "Action": "sts:AssumeRole"}])
	count(iam.deny) == 0 with input as {"resource_changes": [r]} with data.ecp.stack as "redshift"
}

test_trust_of_everyone_denied if {
	r := redshift_role("arn:aws:iam::123456789012:policy/ecp-workload-boundary", [{"Effect": "Allow", "Principal": "*", "Action": "sts:AssumeRole"}])
	count(iam.deny) == 1 with input as {"resource_changes": [r]} with data.ecp.stack as "redshift"
}

test_trust_of_any_aws_principal_denied if {
	r := redshift_role("arn:aws:iam::123456789012:policy/ecp-workload-boundary", [{"Effect": "Allow", "Principal": {"AWS": ["*"]}, "Action": "sts:AssumeRole"}])
	count(iam.deny) == 1 with input as {"resource_changes": [r]} with data.ecp.stack as "redshift"
}

test_trust_of_the_account_root_with_a_condition_allowed if {
	r := redshift_role("arn:aws:iam::123456789012:policy/ecp-workload-boundary", [{"Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::123456789012:root"}, "Action": "sts:AssumeRole", "Condition": {"ArnLike": {"aws:PrincipalArn": "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_AdministratorAccess_*"}}}])
	count(iam.deny) == 0 with input as {"resource_changes": [r]} with data.ecp.stack as "redshift"
}

# The COPY role's read of the shape file and the three gold files is scoped, so no rule denies it.
test_copy_role_scoped_read_allowed if {
	p := rc("aws_iam_role_policy", {"policy": policy_doc([
		{"Effect": "Allow", "Action": "s3:GetObject", "Resource": [
			"arn:aws:s3:::ecp-data-123456789012-ca-central-1/store/runs/*/*/gold/current.parquet",
			"arn:aws:s3:::ecp-data-123456789012-ca-central-1/store/runs/*/*/gold/revisions.parquet",
			"arn:aws:s3:::ecp-data-123456789012-ca-central-1/store/runs/*/*/gold/curves.parquet",
		]},
		{"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::ecp-data-123456789012-ca-central-1", "Condition": {"StringLike": {"s3:prefix": ["store/runs/*/*/gold/curves.parquet"]}}},
	])})
	count(iam.deny) == 0 with input as {"resource_changes": [p]} with data.ecp.stack as "redshift"
}

# AmazonSSMManagedInstanceCore v2's eleven agent actions are explicit, not a wildcard grant.
session_manager_agent_actions := [
	"ssm:UpdateInstanceInformation",
	"ssmmessages:CreateControlChannel",
	"ssmmessages:CreateDataChannel",
	"ssmmessages:OpenControlChannel",
	"ssmmessages:OpenDataChannel",
	"ec2messages:AcknowledgeMessage",
	"ec2messages:DeleteMessage",
	"ec2messages:FailMessage",
	"ec2messages:GetEndpoint",
	"ec2messages:GetMessages",
	"ec2messages:SendReply",
]

test_session_manager_agent_actions_allowed if {
	p := rc("aws_iam_policy", {"policy": policy_doc([{"Effect": "Allow", "Action": session_manager_agent_actions, "Resource": "*"}])})
	count(iam.deny) == 0 with input as {"resource_changes": [p]}
}

test_session_manager_agent_actions_do_not_exempt_wildcard_admin if {
	actions := array.concat(session_manager_agent_actions, ["*"])
	p := rc("aws_iam_policy", {"policy": policy_doc([{"Effect": "Allow", "Action": actions, "Resource": "*"}])})
	count(iam.deny) == 1 with input as {"resource_changes": [p]}
}
