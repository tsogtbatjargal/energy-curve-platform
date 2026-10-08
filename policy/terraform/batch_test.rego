# The rules the batch stack (M4c, ADR-0022) relies on, with the shapes its plan has.
package terraform.batch_test

import data.terraform.iam
import data.terraform.network

rc(address, type, after) := {"address": address, "mode": "managed", "type": type, "change": {"actions": ["create"], "after": after, "after_unknown": {}}}

policy_doc(stmts) := json.marshal({"Version": "2012-10-17", "Statement": stmts})

# --- ADR-0004 batch profile: interface endpoints cost per hour, so only the demo stack has them ---

endpoint(type, stack) := rc("aws_vpc_endpoint.x", "aws_vpc_endpoint", {"vpc_endpoint_type": type, "tags_all": {"stack": stack}})

test_gateway_endpoint_allowed if {
	count(network.deny) == 0 with input as {"resource_changes": [endpoint("Gateway", "batch")]}
}

test_interface_endpoint_outside_demo_denied if {
	every type in ["Interface", "GatewayLoadBalancer", "Resource", "ServiceNetwork"] {
		network.deny == {sprintf("aws_vpc_endpoint.x: %s endpoint outside the demo stack (stack=\"batch\")", [type])} with input as {"resource_changes": [endpoint(type, "batch")]}
	}
}

test_interface_endpoint_in_demo_allowed if {
	count(network.deny) == 0 with input as {"resource_changes": [endpoint("Interface", "demo")]}
}

test_an_endpoint_with_an_unknown_type_denied if {
	e := rc("aws_vpc_endpoint.x", "aws_vpc_endpoint", {"tags_all": {"stack": "batch"}})
	count(network.deny) == 1 with input as {"resource_changes": [e]}
}

# --- ADR-0006: a resource policy never allows every principal ---

resource_policy_types := ["aws_ecr_repository_policy", "aws_s3_bucket_policy", "aws_sns_topic_policy"]

test_resource_policy_allowing_any_principal_denied if {
	every type in resource_policy_types {
		every principal in ["*", {"AWS": "*"}, {"AWS": ["arn:aws:iam::123456789012:root", "*"]}] {
			p := rc(sprintf("%s.x", [type]), type, {"policy": policy_doc([{"Effect": "Allow", "Principal": principal, "Action": "ecr:BatchGetImage"}])})
			iam.deny == {sprintf("%s.x: resource policy allows any principal", [type])} with input as {"resource_changes": [p]}
		}
	}
}

test_resource_policy_for_a_service_principal_allowed if {
	stmt := {
		"Effect": "Allow",
		"Principal": {"Service": "lambda.amazonaws.com"},
		"Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
		"Condition": {"ArnLike": {"aws:sourceARN": "arn:aws:lambda:ca-central-1:123456789012:function:*"}},
	}
	p := rc("aws_ecr_repository_policy.batch", "aws_ecr_repository_policy", {"policy": policy_doc([stmt])})
	count(iam.deny) == 0 with input as {"resource_changes": [p]}
}

test_deny_for_any_principal_allowed if {
	stmt := {"Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": "*", "Condition": {"Bool": {"aws:SecureTransport": "false"}}}
	p := rc("module.data_bucket.aws_s3_bucket_policy.this[0]", "aws_s3_bucket_policy", {"policy": policy_doc([stmt])})
	count(iam.deny) == 0 with input as {"resource_changes": [p]}
}

test_unknown_resource_policy_not_judged if {
	p := {"address": "aws_s3_bucket_policy.x", "mode": "managed", "type": "aws_s3_bucket_policy", "change": {"actions": ["create"], "after": {}, "after_unknown": {"policy": true}}}
	count(iam.deny) == 0 with input as {"resource_changes": [p]}
}

# --- PLAN.md R2 in the batch stack ---

boundary := "arn:aws:iam::123456789012:policy/ecp-workload-boundary"

# The ECS service-linked role cannot carry a boundary; R2 covers the roles the stack defines.
test_service_linked_role_is_not_a_workload_role if {
	slr := rc("aws_iam_service_linked_role.ecs", "aws_iam_service_linked_role", {"aws_service_name": "ecs.amazonaws.com"})
	count(iam.deny) == 0 with input as {"resource_changes": [slr]}
		with data.ecp.stack as "batch"
}

test_batch_roles_need_the_boundary if {
	ok := rc("aws_iam_role.batch[\"task\"]", "aws_iam_role", {"permissions_boundary": boundary})
	bad := rc("aws_iam_role.batch[\"sfn\"]", "aws_iam_role", {})
	iam.deny == {"aws_iam_role.batch[\"sfn\"]: workload roles need permissions_boundary = the ecp-workload-boundary policy ARN, known at plan time (stack batch, PLAN.md R2)"} with input as {"resource_changes": [ok, bad]}
		with data.ecp.stack as "batch"
}

# --- the task's security group: egress 443 only, no ingress ---

test_egress_only_security_group_allowed if {
	sg := rc("aws_security_group.task", "aws_security_group", {"ingress": [], "tags_all": {"stack": "batch"}})
	egress := rc("aws_vpc_security_group_egress_rule.https", "aws_vpc_security_group_egress_rule", {"cidr_ipv4": "0.0.0.0/0", "ip_protocol": "tcp", "from_port": 443, "to_port": 443})
	count(network.deny) == 0 with input as {"resource_changes": [sg, egress]}
}
