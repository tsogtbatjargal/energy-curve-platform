package terraform.network_test

import data.terraform.network

rc(type, after) := {"address": sprintf("%s.x", [type]), "mode": "managed", "type": type, "change": {"actions": ["create"], "after": after}}

plan(rcs) := {"resource_changes": rcs}

test_sg_ssh_open_to_world_denied if {
	sg := rc("aws_security_group", {"ingress": [{"from_port": 22, "to_port": 22, "protocol": "tcp", "cidr_blocks": ["0.0.0.0/0"]}]})
	count(network.deny) == 1 with input as plan([sg])
}

test_sg_https_open_to_world_allowed if {
	sg := rc("aws_security_group", {"ingress": [{"from_port": 443, "to_port": 443, "protocol": "tcp", "cidr_blocks": ["0.0.0.0/0"]}]})
	count(network.deny) == 0 with input as plan([sg])
}

test_sg_port_range_covering_postgres_denied if {
	sg := rc("aws_security_group", {"ingress": [{"from_port": 5000, "to_port": 6000, "protocol": "tcp", "ipv6_cidr_blocks": ["::/0"]}]})
	count(network.deny) == 1 with input as plan([sg])
}

test_sg_all_protocols_denied if {
	sg := rc("aws_security_group", {"ingress": [{"from_port": 0, "to_port": 0, "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"]}]})
	count(network.deny) == 1 with input as plan([sg])
}

test_sg_postgres_from_vpc_allowed if {
	sg := rc("aws_security_group", {"ingress": [{"from_port": 5432, "to_port": 5432, "protocol": "tcp", "cidr_blocks": ["10.0.0.0/16"]}]})
	count(network.deny) == 0 with input as plan([sg])
}

test_sg_rule_resource_denied if {
	r := rc("aws_security_group_rule", {"type": "ingress", "from_port": 3389, "to_port": 3389, "protocol": "tcp", "cidr_blocks": ["0.0.0.0/0"]})
	count(network.deny) == 1 with input as plan([r])
}

test_vpc_ingress_rule_resource_denied if {
	r := rc("aws_vpc_security_group_ingress_rule", {"from_port": 6379, "to_port": 6379, "ip_protocol": "tcp", "cidr_ipv4": "0.0.0.0/0"})
	count(network.deny) == 1 with input as plan([r])
}

test_deleted_resource_ignored if {
	sg := {"address": "aws_security_group.old", "mode": "managed", "type": "aws_security_group", "change": {"actions": ["delete"], "after": null}}
	count(network.deny) == 0 with input as plan([sg])
}

test_nat_in_batch_denied if {
	nat := rc("aws_nat_gateway", {"tags_all": {"stack": "batch"}})
	count(network.deny) == 1 with input as plan([nat])
}

test_nat_in_demo_allowed if {
	nat := rc("aws_nat_gateway", {"tags_all": {"stack": "demo"}})
	count(network.deny) == 0 with input as plan([nat])
}

replaced(type, actions, after) := {"address": sprintf("%s.replaced", [type]), "mode": "managed", "type": type, "change": {"actions": actions, "after": after}}

unchanged_sg := {"address": "aws_security_group.ok", "mode": "managed", "type": "aws_security_group", "change": {"actions": ["no-op"], "after": {"ingress": [{"from_port": 443, "to_port": 443, "protocol": "tcp", "cidr_blocks": ["0.0.0.0/0"]}]}}}

open_ssh := {"ingress": [{"from_port": 22, "to_port": 22, "protocol": "tcp", "cidr_blocks": ["0.0.0.0/0"]}]}

test_replaced_sg_destroy_first_denied if {
	network.deny == {"aws_security_group.replaced: ingress 22-22 open to 0.0.0.0/0"} with input as plan([unchanged_sg, replaced("aws_security_group", ["delete", "create"], open_ssh)])
}

test_replaced_sg_create_first_denied if {
	network.deny == {"aws_security_group.replaced: ingress 22-22 open to 0.0.0.0/0"} with input as plan([unchanged_sg, replaced("aws_security_group", ["create", "delete"], open_ssh)])
}

test_unchanged_violation_still_denied if {
	sg := {"address": "aws_security_group.legacy", "mode": "managed", "type": "aws_security_group", "change": {"actions": ["no-op"], "after": open_ssh}}
	count(network.deny) == 1 with input as plan([sg])
}
