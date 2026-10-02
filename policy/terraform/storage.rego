# ADR-0006 rule 2: every S3 bucket has its own public access block and encryption configuration.
#
# A companion resource (public access block, encryption config) targets a bucket when either:
#   1. both bucket names are known in the plan and equal (existing resources), or
#   2. its `bucket` argument references that bucket in the configuration, in the same module,
#      with a compatible count/for_each instance key.
# New buckets usually need rule 2: the companion's `bucket` value is unknown until apply.
# A companion that matches neither way fails closed rather than being assumed to cover a bucket.
package terraform.storage

import data.terraform.lib

companions := {
	"aws_s3_bucket_public_access_block": "public access block",
	"aws_s3_bucket_server_side_encryption_configuration": "encryption configuration",
}

pab_flags := ["block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"]

# Configuration resources keyed by "<module address>|<type>.<name>".
config_resources[key] := r if {
	walk(input.configuration, [path, value])
	count(path) >= 2
	path[count(path) - 1] == "resources"
	path[count(path) - 2] in {"root_module", "module"}
	some r in value
	key := sprintf("%s|%s", [config_module_address(path), r.address])
}

config_module_address(path) := concat(".", [sprintf("module.%s", [path[i + 1]]) | some i, seg in path; seg == "module_calls"])

bucket_refs(rc) := refs if {
	key := sprintf("%s|%s.%s", [lib.module_address(rc), rc.type, rc.name])
	refs := config_resources[key].expressions.bucket.references
} else := []

known_bucket_name(rc) := name if {
	name := lib.after(rc).bucket
	is_string(name)
}

targets(c, b) if known_bucket_name(c) == known_bucket_name(b)

targets(c, b) if {
	lib.module_address(c) == lib.module_address(b)
	sprintf("%s.%s", [b.type, b.name]) in bucket_refs(c)
	instance_compatible(c, b)
}

# Unindexed bucket: any reference to it is unambiguous.
instance_compatible(_, b) if not lib.has_index(b)

# count/for_each on both sides: instances pair up by key (e.g. bucket = aws_s3_bucket.x[count.index].id).
instance_compatible(c, b) if {
	lib.has_index(b)
	lib.has_index(c)
	c.index == b.index
}

# Unindexed companion pointing at one literal instance (e.g. aws_s3_bucket.x[0].id).
instance_compatible(c, b) if {
	lib.has_index(b)
	sprintf("%s.%s[%s]", [b.type, b.name, json.marshal(b.index)]) in bucket_refs(c)
}

resolvable(c) if known_bucket_name(c)

resolvable(c) if {
	some ref in bucket_refs(c)
	startswith(ref, "aws_s3_bucket.")
}

deny contains msg if {
	some b in lib.resources_of("aws_s3_bucket")
	some ctype, label in companions
	not covered(b, ctype)
	msg := sprintf("%s: no %s targets this bucket", [b.address, label])
}

covered(b, ctype) if {
	some c in lib.resources_of(ctype)
	targets(c, b)
}

deny contains msg if {
	some ctype, label in companions
	some c in lib.resources_of(ctype)
	not resolvable(c)
	msg := sprintf("%s: target bucket unknown at plan time and not traceable through configuration references (%s)", [c.address, label])
}

deny contains msg if {
	some rc in lib.resources_of("aws_s3_bucket_public_access_block")
	some flag in pab_flags
	lib.after(rc)[flag] != true
	msg := sprintf("%s: %s must be true", [rc.address, flag])
}

# A flag that is absent from `after` (unknown at plan time) is not provably true.
deny contains msg if {
	some rc in lib.resources_of("aws_s3_bucket_public_access_block")
	some flag in pab_flags
	not flag in object.keys(lib.after(rc))
	msg := sprintf("%s: %s must be set to true", [rc.address, flag])
}

# ADR-0006 rule 6: databases are never directly reachable from the internet.
public_flag_types := {"aws_db_instance", "aws_rds_cluster_instance", "aws_redshiftserverless_workgroup"}

deny contains msg if {
	some rc in lib.resources
	rc.type in public_flag_types
	lib.after(rc).publicly_accessible == true
	msg := sprintf("%s: publicly_accessible must be false", [rc.address])
}
