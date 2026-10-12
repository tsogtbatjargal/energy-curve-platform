# The job's role (PLAN.md R2, ADR-0018): the workload boundary, a trust for the Glue service only,
# and an inline policy that is the reviewed template in policies/ (tests/test_glue_iam.py evaluates
# it against the boundary offline).
resource "aws_iam_role" "glue" {
  name                 = local.name
  assume_role_policy   = jsonencode(jsondecode(templatefile("${path.module}/policies/glue-trust.json.tftpl", local.policy_values)))
  permissions_boundary = local.boundary_arn
}

# The successful run-scoped Glue logs show HadoopMapRedCommitProtocol with direct writes enabled:
# no _temporary output is written. Its Spark 3.5.6 parent still deletes .spark-staging-<jobId>
# on commit/abort. Limit DeleteObject to that path under the two run outputs; final files and the
# completion record are not deletable. A later Glue run must prove this narrower policy.
# Sources (getStagingDir, commitJob/abortJob, and the mapred protocol's superclass):
# https://github.com/apache/spark/blob/v3.5.6/core/src/main/scala/org/apache/spark/internal/io/FileCommitProtocol.scala#L232-L234
# https://github.com/apache/spark/blob/v3.5.6/core/src/main/scala/org/apache/spark/internal/io/HadoopMapReduceCommitProtocol.scala#L173-L242
# https://github.com/apache/spark/blob/v3.5.6/core/src/main/scala/org/apache/spark/internal/io/HadoopMapRedCommitProtocol.scala#L27-L34
resource "aws_iam_role_policy" "glue" {
  name   = local.name
  role   = aws_iam_role.glue.id
  policy = jsonencode(jsondecode(templatefile("${path.module}/policies/glue-policy.json.tftpl", local.policy_values)))
}
