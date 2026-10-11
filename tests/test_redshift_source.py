"""M5c-1 (ADR-0023): the Redshift stack's source stays inside its reviewed boundaries."""

import re
import subprocess
from pathlib import Path
from typing import Any

import hcl2
import pytest
from hcl2.utils import SerializationOptions

ROOT = Path(__file__).parents[1]
STACK = ROOT / "infra" / "redshift"


def tracked() -> list[str]:
    out = subprocess.run(  # noqa: S603
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "infra/redshift"],  # noqa: S607
        cwd=ROOT, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return sorted(out.stdout.split())


def test_the_stack_holds_exactly_the_reviewed_files() -> None:
    assert tracked() == [
        "infra/redshift/.terraform.lock.hcl",
        "infra/redshift/backend.hcl.example",
        "infra/redshift/backend.tf",
        "infra/redshift/guard.tf",
        "infra/redshift/iam.tf",
        "infra/redshift/locals.tf",
        "infra/redshift/network.tf",
        "infra/redshift/policies/copy-policy.json.tftpl",
        "infra/redshift/policies/copy-trust.json.tftpl",
        "infra/redshift/policies/data-api-policy.json.tftpl",
        "infra/redshift/policies/data-api-trust.json.tftpl",
        "infra/redshift/tests/redshift.tftest.hcl",
        "infra/redshift/variables.tf",
        "infra/redshift/versions.tf",
        "infra/redshift/warehouse.tf",
    ]


def test_no_tfvars_backend_config_state_or_plan_is_tracked() -> None:
    for name in tracked():
        assert not name.endswith((".tfvars", ".tfstate", ".tfplan", "backend.hcl")), name


def text() -> str:
    """The stack's code without its comment lines, which explain the very things it must not do."""
    body = "\n".join(p.read_text() for p in sorted(STACK.glob("*.tf")))
    return re.sub(r"(?m)^\s*#.*$", "", body)


def plain(node: Any) -> Any:
    """hcl2's parse without its bookkeeping key, with string literals unquoted."""
    if isinstance(node, dict):
        return {k: plain(v) for k, v in node.items() if k != "__is_block__"}
    if isinstance(node, list):
        return [plain(v) for v in node]
    if isinstance(node, str) and len(node) > 1 and node.startswith('"') and node.endswith('"'):
        return node[1:-1]
    return node


def blocks(kind: str, type_: str) -> dict[str, dict[str, Any]]:
    """The stack's `resource` blocks of one type, by name."""
    found: dict[str, dict[str, Any]] = {}
    for tf in sorted(STACK.glob("*.tf")):
        with tf.open() as fh:
            doc = hcl2.load(fh, serialization_options=SerializationOptions(with_comments=False))
        for block in doc.get(kind, []):
            for t, named in block.items():
                if t.strip('"') == type_:
                    found |= {n.strip('"'): plain(body) for n, body in named.items()}
    return found


def test_the_stack_has_no_module_call() -> None:
    assert "\nmodule " not in "\n" + text()


def test_the_stack_never_names_the_state_bucket_or_adds_a_second_provider() -> None:
    body = text()
    assert "ecp-tfstate" not in body
    assert body.count("provider ") == 1


@pytest.mark.parametrize(
    "forbidden",
    [
        # IAM only (the Data API): no password, no secret, no admin name, no key
        "admin_user_password", "manage_admin_password", "admin_username",
        "admin_password_secret_kms_key_id", "aws_secretsmanager_", "aws_kms_", "kms_key_id",
        # no way out of the VPC, and no listener
        "aws_internet_gateway", "aws_egress_only_internet_gateway", "aws_nat_gateway",
        "aws_eip", "aws_vpc_endpoint", "aws_route\"", "aws_vpc_security_group_ingress_rule",
        "aws_security_group_rule", "ingress", "0.0.0.0/0",
        # no snapshots, no restore, no sharing, no other warehouse shape
        "snapshot", "aws_redshift_cluster", "aws_redshiftserverless_endpoint_access",
        "aws_redshiftserverless_resource_policy", "aws_redshiftdata_",
        "aws_redshiftserverless_custom_domain", "aws_redshiftserverless_scheduled_action",
        # nothing else of the account
        "aws_lambda_", "aws_sfn_", "aws_glue_", "aws_s3_", "aws_sns_", "aws_cloudwatch_",
        "aws_iam_user", "aws_iam_role_policy_attachment", "aws_iam_policy\"",
        "aws_iam_access_key",
    ],
)  # fmt: skip
def test_the_stack_has_none_of_these(forbidden: str) -> None:
    assert forbidden not in text(), forbidden


def test_the_namespace_sets_only_its_name_database_and_copy_role() -> None:
    (body,) = blocks("resource", "aws_redshiftserverless_namespace").values()
    assert set(body) == {"namespace_name", "db_name", "iam_roles", "depends_on"}


def test_the_workgroup_is_8_rpu_private_and_without_enhanced_vpc_routing() -> None:
    (body,) = blocks("resource", "aws_redshiftserverless_workgroup").values()
    assert body["base_capacity"] == 8 and body["max_capacity"] == 8
    assert body["publicly_accessible"] is False and body["enhanced_vpc_routing"] is False
    assert set(body) == {
        "workgroup_name", "namespace_name", "base_capacity", "max_capacity", "publicly_accessible",
        "enhanced_vpc_routing", "subnet_ids", "security_group_ids", "depends_on",
    }  # fmt: skip


def test_the_usage_limit_is_a_daily_4_rpu_hour_backstop_that_deactivates() -> None:
    (body,) = blocks("resource", "aws_redshiftserverless_usage_limit").values()
    assert body["usage_type"] == "serverless-compute" and body["period"] == "daily"
    assert body["amount"] == 4 and body["breach_action"] == "deactivate"
    assert set(body) == {"resource_arn", "usage_type", "period", "amount", "breach_action"}


def test_the_network_is_a_vpc_with_three_private_subnets_in_three_zones() -> None:
    (vpc,) = blocks("resource", "aws_vpc").values()
    assert vpc["cidr_block"] == "10.43.0.0/16"
    (subnet,) = blocks("resource", "aws_subnet").values()  # one for_each block
    assert subnet["map_public_ip_on_launch"] is False
    assert len(blocks("resource", "aws_route_table")) == 1  # local route only
    assert len(blocks("resource", "aws_route_table_association")) == 1
    assert len(blocks("resource", "aws_security_group")) == 1
    (zones,) = blocks("data", "aws_availability_zones").values()
    assert zones["state"] == "available"
    assert "length(local.zones) == 3" in text()  # three zones are a precondition, not a hope


def test_every_role_has_the_workload_boundary_and_a_reviewed_template() -> None:
    roles = blocks("resource", "aws_iam_role")
    assert set(roles) == {"copy", "caller"}
    for body in roles.values():
        assert "permissions_boundary" in body


def test_the_service_linked_role_is_declared_and_the_warehouse_waits_for_it() -> None:
    (slr,) = blocks("resource", "aws_iam_service_linked_role").values()
    assert slr["aws_service_name"] == "redshift.amazonaws.com"
    for type_ in ("aws_redshiftserverless_namespace", "aws_redshiftserverless_workgroup"):
        (body,) = blocks("resource", type_).values()
        assert any("aws_iam_service_linked_role.redshift" in str(d) for d in body["depends_on"])
