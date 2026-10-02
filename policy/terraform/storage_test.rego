package terraform.storage_test

import data.terraform.storage

rc(type, after) := {"address": sprintf("%s.x", [type]), "mode": "managed", "type": type, "change": {"actions": ["create"], "after": after}}

pab_ok := rc("aws_s3_bucket_public_access_block", {"block_public_acls": true, "block_public_policy": true, "ignore_public_acls": true, "restrict_public_buckets": true})

sse := rc("aws_s3_bucket_server_side_encryption_configuration", {})

bucket := rc("aws_s3_bucket", {})

test_compliant_bucket_allowed if {
	count(storage.deny) == 0 with input as {"resource_changes": [bucket, pab_ok, sse]}
}

test_bucket_without_public_access_block_denied if {
	count(storage.deny) == 1 with input as {"resource_changes": [bucket, sse]}
}

test_bucket_without_encryption_denied if {
	count(storage.deny) == 1 with input as {"resource_changes": [bucket, pab_ok]}
}

test_weak_public_access_block_denied if {
	weak := rc("aws_s3_bucket_public_access_block", {"block_public_acls": true, "block_public_policy": false, "ignore_public_acls": true, "restrict_public_buckets": true})
	count(storage.deny) == 1 with input as {"resource_changes": [bucket, weak, sse]}
}

test_public_rds_denied if {
	count(storage.deny) == 1 with input as {"resource_changes": [rc("aws_db_instance", {"publicly_accessible": true})]}
}

test_private_rds_allowed if {
	count(storage.deny) == 0 with input as {"resource_changes": [rc("aws_db_instance", {"publicly_accessible": false})]}
}

test_public_redshift_workgroup_denied if {
	count(storage.deny) == 1 with input as {"resource_changes": [rc("aws_redshiftserverless_workgroup", {"publicly_accessible": true})]}
}
