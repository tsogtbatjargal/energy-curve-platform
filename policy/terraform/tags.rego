# ADR-0006 rule 3: every taggable resource carries ownership and cost tags.
package terraform.tags

import data.terraform.lib

required := {"project", "owner", "cost-center", "ttl"}

deny contains msg if {
	some rc in lib.resources
	tags := lib.after(rc).tags_all
	is_object(tags)
	missing := required - {k | some k, _ in tags}
	count(missing) > 0
	msg := sprintf("%s: missing tags %v", [rc.address, sort(missing)])
}
