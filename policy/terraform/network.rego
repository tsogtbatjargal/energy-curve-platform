# ADR-0006 rule 1: no admin or database port reachable from the whole internet.
package terraform.network

import data.terraform.lib

admin_ports := {22, 3389, 5432, 6379, 5439}

open_cidrs := {"0.0.0.0/0", "::/0"}

exposes(_, _, protocol) if {
	protocol in {"-1", "all"}
}

exposes(from, to, protocol) if {
	not protocol in {"-1", "all"}
	some port in admin_ports
	from <= port
	port <= to
}

deny contains msg if {
	some rc in lib.resources_of("aws_security_group")
	some rule in lib.after(rc).ingress
	some cidr in array.concat(object.get(rule, "cidr_blocks", []), object.get(rule, "ipv6_cidr_blocks", []))
	cidr in open_cidrs
	exposes(rule.from_port, rule.to_port, rule.protocol)
	msg := sprintf("%s: ingress %v-%v open to %s", [rc.address, rule.from_port, rule.to_port, cidr])
}

deny contains msg if {
	some rc in lib.resources_of("aws_security_group_rule")
	a := lib.after(rc)
	a.type == "ingress"
	some cidr in array.concat(object.get(a, "cidr_blocks", []), object.get(a, "ipv6_cidr_blocks", []))
	cidr in open_cidrs
	exposes(a.from_port, a.to_port, a.protocol)
	msg := sprintf("%s: ingress %v-%v open to %s", [rc.address, a.from_port, a.to_port, cidr])
}

deny contains msg if {
	some rc in lib.resources_of("aws_vpc_security_group_ingress_rule")
	a := lib.after(rc)
	some cidr in [object.get(a, "cidr_ipv4", null), object.get(a, "cidr_ipv6", null)]
	cidr in open_cidrs
	exposes(object.get(a, "from_port", 0), object.get(a, "to_port", 65535), a.ip_protocol)
	msg := sprintf("%s: ingress open to %s on an admin/database port", [rc.address, cidr])
}

# ADR-0006 rule 5: NAT gateways cost ~$1/day each; only the short-lived demo stack may have one.
deny contains msg if {
	some rc in lib.resources_of("aws_nat_gateway")
	stack := object.get(object.get(lib.after(rc), "tags_all", {}), "stack", "")
	stack != "demo"
	msg := sprintf("%s: NAT gateway outside the demo stack (stack=%q)", [rc.address, stack])
}

# ADR-0004 batch profile: no interface endpoints (each costs per hour and per AZ); the batch task
# reaches AWS APIs over the internet gateway and S3 through a free gateway endpoint. Only the demo
# stack may have other endpoint types. A type unknown at plan time is not provably a gateway.
deny contains msg if {
	some rc in lib.resources_of("aws_vpc_endpoint")
	type := object.get(lib.after(rc), "vpc_endpoint_type", "unknown")
	type != "Gateway"
	stack := object.get(object.get(lib.after(rc), "tags_all", {}), "stack", "")
	stack != "demo"
	msg := sprintf("%s: %s endpoint outside the demo stack (stack=%q)", [rc.address, type, stack])
}
