# The seasonal-shape job (ADR-0007, ADR-0023). Glue 5.1 is Python 3.11 and Spark 3.5.6, the
# versions CI's glue-compat job pins. No VPC, connection, crawler, catalog or trigger: it generates
# synthetic data inside Spark and writes two small files. On demand, 2 workers, a 10-minute cap and
# no retries bound one run to about $0.15.
resource "aws_glue_job" "shape" {
  name              = local.name
  role_arn          = aws_iam_role.glue.arn
  glue_version      = "5.1"
  worker_type       = "G.1X"
  number_of_workers = 2
  timeout           = 10
  max_retries       = 0
  execution_class   = "STANDARD"

  # The names above are constants, so the plan can show them; the objects must exist first.
  depends_on = [aws_s3_object.script, aws_s3_object.bundle]

  command {
    name            = "glueetl"
    python_version  = "3"
    script_location = "s3://${local.bucket}/${local.script_key}"
  }

  execution_property {
    max_concurrent_runs = 1
  }

  default_arguments = {
    "--job-language"                     = "python"
    "--extra-py-files"                   = "s3://${local.bucket}/${local.bundle_key}"
    "--enable-metrics"                   = ""
    "--enable-continuous-cloudwatch-log" = "true"
    "--continuous-log-logGroup"          = aws_cloudwatch_log_group.glue.name
    "--enable-continuous-log-filter"     = "true"
    "--start"                            = "1983-01-03"
    "--end"                              = "2024-04-05"
    "--window-start"                     = "2014-01-01"
    "--window-end"                       = "2024-04-05"
    "--partitions"                       = "8"
    "--output"                           = local.output_url
  }
}
