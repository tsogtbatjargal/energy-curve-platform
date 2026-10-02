package terraform.storage_test

import data.terraform.storage

pab_flags := {"block_public_acls": true, "block_public_policy": true, "ignore_public_acls": true, "restrict_public_buckets": true}

res(type, name, actions, after) := {"address": sprintf("%s.%s", [type, name]), "mode": "managed", "type": type, "name": name, "change": {"actions": actions, "after": after}}

bucket(name, bucket_name) := res("aws_s3_bucket", name, ["create"], {"bucket": bucket_name})

pab(name, after) := res("aws_s3_bucket_public_access_block", name, ["create"], object.union(pab_flags, after))

sse(name, after) := res("aws_s3_bucket_server_side_encryption_configuration", name, ["create"], after)

cfg(type, name, refs) := {"address": sprintf("%s.%s", [type, name]), "type": type, "name": name, "expressions": {"bucket": {"references": refs}}}

root_config(resources) := {"root_module": {"resources": resources}}

msgs_for(msgs, addr) := {m | some m in msgs; startswith(m, sprintf("%s:", [addr]))}

# --- known names (existing resources) ---

test_known_names_compliant if {
	count(storage.deny) == 0 with input as {"resource_changes": [
		bucket("a", "bucket-a"),
		pab("a", {"bucket": "bucket-a"}),
		sse("a", {"bucket": "bucket-a"}),
	]}
}

test_mismatched_known_names_equal_counts_still_denied if {
	# Two buckets, two blocks, two encryption configs, but all target bucket-a.
	# A count-based check passes this; per-bucket matching must flag bucket b.
	msgs := storage.deny with input as {"resource_changes": [
		bucket("a", "bucket-a"), bucket("b", "bucket-b"),
		pab("a", {"bucket": "bucket-a"}), pab("a2", {"bucket": "bucket-a"}),
		sse("a", {"bucket": "bucket-a"}), sse("a2", {"bucket": "bucket-a"}),
	]}
	msgs == {
		"aws_s3_bucket.b: no public access block targets this bucket",
		"aws_s3_bucket.b: no encryption configuration targets this bucket",
	}
}

# --- unknown names (new resources): matched through configuration references ---

test_unknown_names_resolved_through_references if {
	count(storage.deny) == 0 with input as {
		"resource_changes": [bucket("a", null), pab("a", {}), sse("a", {})],
		"configuration": root_config([
			cfg("aws_s3_bucket_public_access_block", "a", ["aws_s3_bucket.a.id", "aws_s3_bucket.a"]),
			cfg("aws_s3_bucket_server_side_encryption_configuration", "a", ["aws_s3_bucket.a.id", "aws_s3_bucket.a"]),
		]),
	}
}

test_mismatched_references_denied if {
	msgs := storage.deny with input as {
		"resource_changes": [bucket("a", null), bucket("b", null), pab("pa", {}), pab("pb", {}), sse("sa", {}), sse("sb", {})],
		"configuration": root_config([
			cfg("aws_s3_bucket_public_access_block", "pa", ["aws_s3_bucket.a"]),
			cfg("aws_s3_bucket_public_access_block", "pb", ["aws_s3_bucket.a"]),
			cfg("aws_s3_bucket_server_side_encryption_configuration", "sa", ["aws_s3_bucket.a"]),
			cfg("aws_s3_bucket_server_side_encryption_configuration", "sb", ["aws_s3_bucket.b"]),
		]),
	}
	msgs == {"aws_s3_bucket.b: no public access block targets this bucket"}
}

test_unresolved_reference_fails_closed if {
	# Block's bucket is unknown and its configuration points at a variable, not a bucket.
	msgs := storage.deny with input as {
		"resource_changes": [bucket("a", null), pab("a", {}), sse("a", {})],
		"configuration": root_config([
			cfg("aws_s3_bucket_public_access_block", "a", ["var.bucket_id"]),
			cfg("aws_s3_bucket_server_side_encryption_configuration", "a", ["aws_s3_bucket.a"]),
		]),
	}
	msgs == {
		"aws_s3_bucket.a: no public access block targets this bucket",
		"aws_s3_bucket_public_access_block.a: target bucket unknown at plan time and not traceable through configuration references (public access block)",
	}
}

test_missing_configuration_fails_closed if {
	msgs := storage.deny with input as {"resource_changes": [bucket("a", null), pab("a", {}), sse("a", {})]}
	count(msgs_for(msgs, "aws_s3_bucket.a")) == 2
	count(msgs_for(msgs, "aws_s3_bucket_public_access_block.a")) == 1
	count(msgs_for(msgs, "aws_s3_bucket_server_side_encryption_configuration.a")) == 1
}

# --- count / for_each instances and module scoping ---

test_count_instances_pair_by_index if {
	b0 := object.union(bucket("x", null), {"address": "aws_s3_bucket.x[0]", "index": 0})
	b1 := object.union(bucket("x", null), {"address": "aws_s3_bucket.x[1]", "index": 1})
	p0 := object.union(pab("x", {}), {"address": "aws_s3_bucket_public_access_block.x[0]", "index": 0})
	s0 := object.union(sse("x", {}), {"address": "aws_s3_bucket_server_side_encryption_configuration.x[0]", "index": 0})
	s1 := object.union(sse("x", {}), {"address": "aws_s3_bucket_server_side_encryption_configuration.x[1]", "index": 1})
	msgs := storage.deny with input as {
		"resource_changes": [b0, b1, p0, s0, s1],
		"configuration": root_config([
			cfg("aws_s3_bucket_public_access_block", "x", ["aws_s3_bucket.x", "count.index"]),
			cfg("aws_s3_bucket_server_side_encryption_configuration", "x", ["aws_s3_bucket.x", "count.index"]),
		]),
	}
	msgs == {"aws_s3_bucket.x[1]: no public access block targets this bucket"}
}

test_module_literal_instance_and_module_scoping if {
	mb := object.union(bucket("this", null), {"address": "module.m.aws_s3_bucket.this[0]", "module_address": "module.m", "index": 0})
	mp := object.union(pab("this", {}), {"address": "module.m.aws_s3_bucket_public_access_block.this", "module_address": "module.m"})
	ms := object.union(sse("this", {}), {"address": "module.m.aws_s3_bucket_server_side_encryption_configuration.this", "module_address": "module.m"})

	# Same type and name in another module: module m's companions must not cover it.
	ob := object.union(bucket("this", null), {"address": "module.other.aws_s3_bucket.this[0]", "module_address": "module.other", "index": 0})
	refs := ["aws_s3_bucket.this[0].id", "aws_s3_bucket.this[0]", "aws_s3_bucket.this"]
	msgs := storage.deny with input as {
		"resource_changes": [mb, mp, ms, ob],
		"configuration": {"root_module": {"module_calls": {"m": {"module": {"resources": [
			cfg("aws_s3_bucket_public_access_block", "this", refs),
			cfg("aws_s3_bucket_server_side_encryption_configuration", "this", refs),
		]}}}}},
	}
	msgs == {
		"module.other.aws_s3_bucket.this[0]: no public access block targets this bucket",
		"module.other.aws_s3_bucket.this[0]: no encryption configuration targets this bucket",
	}
}

# --- replacements alongside unchanged resources ---

replacement_plan(order) := {"resource_changes": [
	res("aws_s3_bucket", "keep", ["no-op"], {"bucket": "keep"}),
	res("aws_s3_bucket_public_access_block", "keep", ["no-op"], object.union(pab_flags, {"bucket": "keep"})),
	res("aws_s3_bucket_server_side_encryption_configuration", "keep", ["no-op"], {"bucket": "keep"}),
	res("aws_s3_bucket", "swap", order, {"bucket": "swap"}),
]}

test_replaced_bucket_destroy_first_is_checked if {
	msgs := storage.deny with input as replacement_plan(["delete", "create"])
	count(msgs) == 2
	count(msgs_for(msgs, "aws_s3_bucket.swap")) == 2
}

test_replaced_bucket_create_first_is_checked if {
	msgs := storage.deny with input as replacement_plan(["create", "delete"])
	count(msgs) == 2
	count(msgs_for(msgs, "aws_s3_bucket.swap")) == 2
}

test_replaced_weak_block_is_checked if {
	weak := res("aws_s3_bucket_public_access_block", "a", ["create", "delete"], object.union(pab_flags, {"bucket": "a", "block_public_policy": false}))
	msgs := storage.deny with input as {"resource_changes": [bucket("a", "a"), weak, sse("a", {"bucket": "a"})]}
	msgs == {"aws_s3_bucket_public_access_block.a: block_public_policy must be true"}
}

test_deleted_bucket_ignored if {
	count(storage.deny) == 0 with input as {"resource_changes": [res("aws_s3_bucket", "old", ["delete"], null)]}
}

test_unknown_flag_denied if {
	partial := res("aws_s3_bucket_public_access_block", "a", ["create"], {"bucket": "a", "block_public_acls": true, "ignore_public_acls": true, "restrict_public_buckets": true})
	msgs := storage.deny with input as {"resource_changes": [bucket("a", "a"), partial, sse("a", {"bucket": "a"})]}
	msgs == {"aws_s3_bucket_public_access_block.a: block_public_policy must be set to true"}
}

# --- real `terraform show -json` shape (module, count, unknown names; no explicit encryption) ---

test_real_plan_fixture if {
	msgs := storage.deny with input as data.terraform.testdata.s3_mixed_plan
	msgs == {
		"aws_s3_bucket.counted[0]: no encryption configuration targets this bucket",
		"aws_s3_bucket.counted[1]: no encryption configuration targets this bucket",
		"aws_s3_bucket.unnamed: no encryption configuration targets this bucket",
		"module.data_bucket.aws_s3_bucket.this[0]: no encryption configuration targets this bucket",
	}
}

# --- databases ---

test_public_rds_denied if {
	count(storage.deny) == 1 with input as {"resource_changes": [res("aws_db_instance", "db", ["create"], {"publicly_accessible": true})]}
}

test_private_rds_allowed if {
	count(storage.deny) == 0 with input as {"resource_changes": [res("aws_db_instance", "db", ["create"], {"publicly_accessible": false})]}
}

test_public_redshift_workgroup_replaced_denied if {
	count(storage.deny) == 1 with input as {"resource_changes": [res("aws_redshiftserverless_workgroup", "wg", ["delete", "create"], {"publicly_accessible": true})]}
}
