package terraform.tags_test

import data.terraform.tags

rc(after) := {"address": "aws_s3_bucket.x", "mode": "managed", "type": "aws_s3_bucket", "change": {"actions": ["create"], "after": after}}

full := {"project": "p", "owner": "o", "cost-center": "c", "ttl": "permanent"}

test_fully_tagged_allowed if {
	count(tags.deny) == 0 with input as {"resource_changes": [rc({"tags_all": full})]}
}

test_missing_ttl_denied if {
	msgs := tags.deny with input as {"resource_changes": [rc({"tags_all": object.remove(full, ["ttl"])})]}
	count(msgs) == 1
	some m in msgs
	contains(m, "ttl")
}

test_untaggable_resource_ignored if {
	count(tags.deny) == 0 with input as {"resource_changes": [rc({"bucket": "b"})]}
}
