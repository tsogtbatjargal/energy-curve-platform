# Shared helpers over `terraform show -json` plan output.
package terraform.lib

# Actions after which the resource no longer exists or is no longer managed. Replacements
# (["delete","create"] and ["create","delete"]) are live: the new object must be checked.
# Anything else, including action types added in future Terraform versions, is treated as live
# so the gate fails closed.
gone_actions := {["delete"], ["forget"]}

live(rc) if {
	rc.mode == "managed"
	not rc.change.actions in gone_actions
}

resources contains rc if {
	some rc in input.resource_changes
	live(rc)
}

resources_of(type) := {rc | some rc in resources; rc.type == type}

after(rc) := rc.change.after

# Root-module resources have no module_address (or null); normalise to "".
module_address(rc) := m if {
	m := rc.module_address
	is_string(m)
} else := ""

has_index(rc) if {
	"index" in object.keys(rc)
	rc.index != null
}
