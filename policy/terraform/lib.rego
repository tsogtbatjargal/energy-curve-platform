# Shared helpers over `terraform show -json` plan output.
# Every resource that will exist after apply is evaluated, including unchanged (no-op) ones,
# so a pre-existing violation still fails the gate.
package terraform.lib

resources contains rc if {
	some rc in input.resource_changes
	rc.mode == "managed"
	not "delete" in rc.change.actions
}

resources_of(type) := {rc | some rc in resources; rc.type == type}

after(rc) := rc.change.after
