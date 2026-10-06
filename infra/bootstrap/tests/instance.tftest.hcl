# ADR-0021 phase 3: one stack, two instances (the management account, ecp-workloads). Offline: the
# AWS provider is mocked, so these plans make no AWS call. Account IDs are placeholders.
#   terraform -chdir=infra/bootstrap init -backend=false && terraform -chdir=infra/bootstrap test

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
  mock_data "aws_organizations_organization" {
    defaults = { master_account_id = "111111111111" }
  }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

variables {
  expected_account_id = "111111111111"
}

# --- the management instance (defaults) --------------------------------------------------------

run "management_defaults_create_no_member_resources" {
  command = plan

  assert {
    condition     = length(aws_s3_bucket.member_state) == 0 && length(aws_iam_openid_connect_provider.github) == 0 && length(aws_iam_role.break_glass) == 0
    error_message = "The default (management) instance must not create the bucket, the OIDC provider or the break-glass role."
  }
  assert {
    condition     = length(data.aws_iam_openid_connect_provider.github) == 1
    error_message = "The management instance must reference the existing OIDC provider."
  }
}

run "management_instance_refuses_another_account" {
  command = plan
  variables {
    expected_account_id = "222222222222"
  }
  expect_failures = [data.aws_caller_identity.current]
}

run "member_flag_refused_in_the_management_account" {
  command = plan
  variables {
    member_instance = true
  }
  expect_failures = [data.aws_organizations_organization.this]
}

# --- the member instance -----------------------------------------------------------------------

run "member_instance_creates_its_own_bucket_provider_and_break_glass" {
  command = plan
  variables {
    expected_account_id = "333333333333"
    member_instance     = true
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "333333333333" }
  }
  # Mock providers cannot import; stand in for the role Organizations created.
  override_resource {
    target = aws_iam_role.break_glass["OrganizationAccountAccessRole"]
    values = { arn = "arn:aws:iam::333333333333:role/OrganizationAccountAccessRole" }
  }

  assert {
    condition     = aws_s3_bucket.member_state[0].bucket == "ecp-tfstate-333333333333-ca-central-1"
    error_message = "The member bucket must be named for the member account."
  }
  assert {
    condition     = length(aws_iam_openid_connect_provider.github) == 1 && length(data.aws_iam_openid_connect_provider.github) == 0
    error_message = "The member instance must create the OIDC provider, not read one."
  }
  assert {
    condition     = keys(aws_iam_role.break_glass) == ["OrganizationAccountAccessRole"]
    error_message = "The member instance manages exactly OrganizationAccountAccessRole."
  }
  assert {
    condition     = aws_iam_role.break_glass["OrganizationAccountAccessRole"].max_session_duration == 3600
    error_message = "The break-glass role keeps 1-hour sessions."
  }
  assert {
    condition     = jsondecode(aws_iam_role.break_glass["OrganizationAccountAccessRole"].assume_role_policy).Statement[0].Principal.AWS == "arn:aws:iam::111111111111:root"
    error_message = "The break-glass role trusts the management account only."
  }
}

run "member_instance_refuses_another_account" {
  command = plan
  variables {
    expected_account_id = "444444444444"
    member_instance     = true
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "333333333333" }
  }
  # Mock providers cannot import; stand in for the role Organizations created.
  override_resource {
    target = aws_iam_role.break_glass["OrganizationAccountAccessRole"]
    values = { arn = "arn:aws:iam::333333333333:role/OrganizationAccountAccessRole" }
  }
  expect_failures = [data.aws_caller_identity.current]
}

run "management_flags_refused_in_a_member_account" {
  command = plan
  variables {
    expected_account_id = "333333333333"
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "333333333333" }
  }
  expect_failures = [data.aws_organizations_organization.this]
}

run "expected_account_must_be_twelve_digits" {
  command = plan
  variables {
    expected_account_id = "not-an-account"
  }
  expect_failures = [var.expected_account_id]
}
