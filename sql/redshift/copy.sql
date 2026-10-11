-- ADR-0023: Redshift-only. COPY from S3 treats its path as a key PREFIX (COPY from Amazon S3: a
-- name like custdata.txt also matches custdata.txt.1 and custdata.txt.bak), so every statement
-- names one full key, and the loader checks before each COPY that exactly one object matches it
-- with the recorded size. The role is the namespace's ecp-redshift-copy. The placeholders are
-- filled by scripts/redshift_load.py from validated values; nothing is a wildcard or a prefix.
--
-- The files are the Glue job's CSV (header line skipped) and the published gold parquet files.

-- name: copy_shape_params
COPY public.ecp_shape_params FROM '{shape_uri}' IAM_ROLE '{role_arn}' FORMAT AS CSV IGNOREHEADER 1;

-- name: copy_observations
COPY public.ecp_observations FROM '{observations_uri}' IAM_ROLE '{role_arn}' FORMAT AS PARQUET;

-- name: copy_revisions
COPY public.ecp_revisions FROM '{revisions_uri}' IAM_ROLE '{role_arn}' FORMAT AS PARQUET;

-- name: copy_curve_points
COPY public.ecp_curve_points FROM '{curves_uri}' IAM_ROLE '{role_arn}' FORMAT AS PARQUET;
