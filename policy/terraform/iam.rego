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

# Policies whose JSON depends on not-yet-created resources are unknown at plan time.
warn contains msg if {
	some rc in input.resource_changes
	rc.type in policy_types
	rc.change.after_unknown.policy == true
	msg := sprintf("%s: policy JSON unknown at plan time; workload stacks must satisfy PLAN.md R1 before apply", [rc.address])
}
