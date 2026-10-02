# ADR-0006 rule 2: S3 buckets block public access and are encrypted.
package terraform.storage

import data.terraform.lib

pab_flags := ["block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"]

deny contains msg if {
	some rc in lib.resources_of("aws_s3_bucket_public_access_block")
	some flag in pab_flags
	lib.after(rc)[flag] != true
	msg := sprintf("%s: %s must be true", [rc.address, flag])
}

# Bucket-to-config matching is by count: plan JSON often lacks known bucket IDs at plan time.
deny contains msg if {
	buckets := count(lib.resources_of("aws_s3_bucket"))
	blocks := count(lib.resources_of("aws_s3_bucket_public_access_block"))
	blocks < buckets
	msg := sprintf("%d S3 bucket(s) but only %d public access block(s)", [buckets, blocks])
}

deny contains msg if {
	buckets := count(lib.resources_of("aws_s3_bucket"))
	sse := count(lib.resources_of("aws_s3_bucket_server_side_encryption_configuration"))
	sse < buckets
	msg := sprintf("%d S3 bucket(s) but only %d encryption configuration(s)", [buckets, sse])
}

# ADR-0006 rule 6: databases are never directly reachable from the internet.
public_flag_types := {"aws_db_instance", "aws_rds_cluster_instance", "aws_redshiftserverless_workgroup"}

deny contains msg if {
	some rc in lib.resources
	rc.type in public_flag_types
	lib.after(rc).publicly_accessible == true
	msg := sprintf("%s: publicly_accessible must be false", [rc.address])
}
