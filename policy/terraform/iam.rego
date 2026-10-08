# ADR-0006 rule 4: no wildcard admin grants.
package terraform.iam

import data.terraform.lib

policy_types := {"aws_iam_policy", "aws_iam_role_policy", "aws_iam_user_policy", "aws_iam_group_policy"}

as_list(x) := x if is_array(x)

as_list(x) := [x] if not is_array(x)

deny contains msg if {
	some rc in lib.resources
	rc.type in policy_types
	doc := json.unmarshal(lib.after(rc).policy)
	some stmt in as_list(doc.Statement)
	stmt.Effect == "Allow"
	"*" in as_list(stmt.Action)
	"*" in as_list(object.get(stmt, "Resource", []))
	msg := sprintf("%s: statement allows * on *", [rc.address])
}

deny contains msg if {
	some rc in lib.resources
	rc.type in {"aws_iam_role_policy_attachment", "aws_iam_user_policy_attachment", "aws_iam_group_policy_attachment"}
	lib.after(rc).policy_arn == "arn:aws:iam::aws:policy/AdministratorAccess"
	msg := sprintf("%s: AdministratorAccess attachment is not allowed", [rc.address])
}

# ADR-0006: a resource policy never allows every principal. Deny statements may (the bucket's
# deny-non-TLS statement does). A policy unknown at plan time is not judged here.
resource_policy_types := {"aws_ecr_repository_policy", "aws_s3_bucket_policy", "aws_sns_topic_policy"}

any_principal(p) if p == "*"

any_principal(p) if "*" in as_list(p.AWS)

deny contains msg if {
	some rc in lib.resources
	rc.type in resource_policy_types
	is_string(lib.after(rc).policy)
	doc := json.unmarshal(lib.after(rc).policy)
	some stmt in as_list(doc.Statement)
	stmt.Effect == "Allow"
	any_principal(stmt.Principal)
	msg := sprintf("%s: resource policy allows any principal", [rc.address])
}

# Policies whose JSON depends on not-yet-created resources are unknown at plan time. For workload
# stacks scripts/iam_approval.py (PLAN.md R1, ADR-0017) fails them unless a reviewed approval
# matches their fingerprint; this warning keeps them visible in every stack's report.
warn contains msg if {
	some rc in input.resource_changes
	rc.type in policy_types
	rc.change.after_unknown.policy == true
	msg := sprintf("%s: policy JSON unknown at plan time; see the R1 approval check below", [rc.address])
}

# PLAN.md R2 (ADR-0018): every role a workload stack creates carries the workload boundary, so
# what the deploy role creates can never exceed it. policy_gate.py passes the stack name as
# data.ecp.stack; without it the stack counts as a workload stack, so the rule fails closed.
boundary_exempt_stacks := {"bootstrap"}

stack := s if {
	s := data.ecp.stack
} else := "MISSING"

boundary_arn := `^arn:aws:iam::[0-9]{12}:policy/ecp-workload-boundary$`

valid_boundary(rc) if {
	b := lib.after(rc).permissions_boundary
	is_string(b)
	regex.match(boundary_arn, b)
}

deny contains msg if {
	not stack in boundary_exempt_stacks
	some rc in lib.resources_of("aws_iam_role")
	not valid_boundary(rc)
	msg := sprintf("%s: workload roles need permissions_boundary = the ecp-workload-boundary policy ARN, known at plan time (stack %s, PLAN.md R2)", [rc.address, stack])
}
