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
