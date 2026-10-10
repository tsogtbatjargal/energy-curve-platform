# M5b (ADR-0023): the Glue stack planned against a mocked AWS provider. Offline: no credentials,
# no backend, no AWS call. Account IDs are placeholders.
#   python scripts/build_glue_bundle.py && terraform -chdir=infra/glue init -backend=false &&
#   terraform -chdir=infra/glue test

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "333333333333" }
  }
  mock_data "aws_organizations_organization" {
    defaults = { master_account_id = "111111111111" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::333333333333:role/ecp-glue-shape" }
  }
}

variables {
  expected_account_id = "333333333333"
}

run "the_job_is_the_reviewed_one" {
  command = apply

  assert {
    condition     = aws_glue_job.shape.glue_version == "5.1" && aws_glue_job.shape.worker_type == "G.1X" && aws_glue_job.shape.number_of_workers == 2
    error_message = "Glue 5.1 on two G.1X workers (ADR-0007, ADR-0023)."
  }
  assert {
    condition     = aws_glue_job.shape.timeout == 10 && aws_glue_job.shape.max_retries == 0 && aws_glue_job.shape.execution_property[0].max_concurrent_runs == 1
    error_message = "A 10-minute cap, no retries and one run at a time bound the cost of a run."
  }
  assert {
    condition     = aws_glue_job.shape.command[0].name == "glueetl" && aws_glue_job.shape.command[0].script_location == "s3://ecp-glue-333333333333-ca-central-1/glue/shape_job.py"
    error_message = "A Spark ETL job whose script is the object this stack uploads."
  }
  assert {
    condition     = aws_glue_job.shape.connections == null && aws_glue_job.shape.security_configuration == null
    error_message = "No connection (so no VPC) and no security configuration."
  }
}

run "the_arguments_are_exactly_the_reviewed_ones" {
  command = apply

  assert {
    condition = jsonencode(aws_glue_job.shape.default_arguments) == jsonencode({
      "--job-language"                     = "python"
      "--extra-py-files"                   = "s3://ecp-glue-333333333333-ca-central-1/glue/energy_curves_m5.zip"
      "--enable-metrics"                   = ""
      "--enable-continuous-cloudwatch-log" = "true"
      "--continuous-log-logGroup"          = "/aws-glue/ecp-shape"
      "--enable-continuous-log-filter"     = "true"
      "--output"                           = "s3://ecp-glue-333333333333-ca-central-1/m5/shape"
      "--partitions"                       = "8"
    })
    error_message = "No Data Catalog flag, no temp dir, no PyPI modules: the job needs only its bundle."
  }
}

run "the_role_carries_the_boundary" {
  command = apply

  assert {
    condition     = aws_iam_role.glue.name == "ecp-glue-shape" && aws_iam_role.glue.permissions_boundary == "arn:aws:iam::333333333333:policy/ecp-workload-boundary"
    error_message = "The role is ecp-glue-shape and carries the workload boundary (PLAN.md R2)."
  }
}

run "the_wrong_region_is_refused" {
  command = plan
  variables {
    region = "us-east-1"
  }
  expect_failures = [var.region]
}
