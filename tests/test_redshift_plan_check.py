"""M5c-1 (ADR-0023): the Redshift stack's plan must be exactly the reviewed shape before its apply.

The plan is synthetic, shaped like the Glue stack's real `terraform show -json` (Terraform 1.15.8).
Which attributes the AWS provider leaves unknown on a create (ids, ARNs, computed defaults) is
modelled from the documentation and the provider schema; the first real plan confirms it before its
hash is reported. Account IDs are placeholders.
"""

import copy
import json
from pathlib import Path
from typing import Any

import policy_templates
import pytest
import redshift_plan_check as rpc

ROOT = Path(__file__).parents[1]
MEMBER, MANAGEMENT, REGION = "333333333333", "111111111111", "ca-central-1"
BOUNDARY = f"arn:aws:iam::{MEMBER}:policy/ecp-workload-boundary"
POLICIES = ROOT / "infra" / "redshift" / "policies"
VALUES = {"account_id": MEMBER, "region": REGION}
TAGS = {"project": "energy-curve-platform", "owner": "o", "cost-center": "personal",
        "ttl": "permanent", "stack": "redshift"}  # fmt: skip
ZONES = ["ca-central-1a", "ca-central-1b", "ca-central-1d"]
KEYS = ["a", "b", "c"]
VPC, DSG, SG, RT = (
    "aws_vpc.redshift", "aws_default_security_group.redshift",
    "aws_security_group.redshift", "aws_route_table.private",
)  # fmt: skip
SUBNETS = [f'aws_subnet.private["{k}"]' for k in KEYS]
ASSOCS = [f'aws_route_table_association.private["{k}"]' for k in KEYS]
COPY, CALLER = "aws_iam_role.copy", "aws_iam_role.caller"
COPY_POLICY, CALLER_POLICY = "aws_iam_role_policy.copy", "aws_iam_role_policy.caller"
SLR = "aws_iam_service_linked_role.redshift"
NS, WG, LIMIT = (
    "aws_redshiftserverless_namespace.this", "aws_redshiftserverless_workgroup.this",
    "aws_redshiftserverless_usage_limit.this",
)  # fmt: skip
ADDRESSES = [VPC, DSG, *SUBNETS, RT, *ASSOCS, SG, COPY, CALLER, COPY_POLICY, CALLER_POLICY, SLR,
             NS, WG, LIMIT]  # fmt: skip


def template(name: str) -> str:
    return json.dumps(policy_templates.render(name, VALUES, POLICIES))


def rc(address: str, after: dict[str, Any], unknown: dict[str, Any]) -> dict[str, Any]:
    return {
        "address": address, "mode": "managed", "type": address.split(".")[0],
        "change": {"actions": ["create"], "before": None, "after": after,
                   "after_unknown": unknown},
    }  # fmt: skip


def resources() -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """address -> (after, after_unknown) of the reviewed create."""
    out: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
        VPC: ({"cidr_block": "10.43.0.0/16", "enable_dns_support": True,
               "enable_dns_hostnames": True}, {"id": True}),
        DSG: ({}, {"id": True, "vpc_id": True, "ingress": True, "egress": True}),
        RT: ({}, {"id": True, "vpc_id": True, "route": True}),
        SG: ({"name": "ecp-redshift"},
             {"id": True, "vpc_id": True, "ingress": True, "egress": True}),
        COPY: ({"name": "ecp-redshift-copy", "permissions_boundary": BOUNDARY,
                "assume_role_policy": template("copy-trust")}, {"arn": True, "id": True}),
        CALLER: ({"name": "ecp-redshift-data-api", "permissions_boundary": BOUNDARY,
                  "assume_role_policy": template("data-api-trust")}, {"arn": True, "id": True}),
        COPY_POLICY: ({"name": "ecp-redshift-copy", "policy": template("copy-policy")},
                      {"id": True, "role": True}),
        CALLER_POLICY: ({"name": "ecp-redshift-data-api", "policy": template("data-api-policy")},
                        {"id": True, "role": True}),
        SLR: ({"aws_service_name": "redshift.amazonaws.com"}, {"arn": True, "id": True}),
        NS: ({"namespace_name": "ecp-redshift", "db_name": "ecp", "admin_username": None,
              "admin_user_password": None, "admin_user_password_wo": None,
              "admin_user_password_wo_version": None, "manage_admin_password": None,
              "admin_password_secret_kms_key_id": None, "default_iam_role_arn": None,
              "kms_key_id": None, "log_exports": None},
             {"id": True, "arn": True, "iam_roles": [True], "admin_username": True,
              "kms_key_id": True}),
        WG: ({"workgroup_name": "ecp-redshift", "namespace_name": "ecp-redshift",
              "base_capacity": 8, "max_capacity": 8, "publicly_accessible": False,
              "enhanced_vpc_routing": False, "tags_all": TAGS},
             {"id": True, "arn": True, "subnet_ids": [True, True, True],
              "security_group_ids": [True], "config_parameter": True, "endpoint": True}),
        LIMIT: ({"usage_type": "serverless-compute", "period": "daily", "amount": 4,
                 "breach_action": "deactivate"}, {"id": True, "resource_arn": True}),
    }  # fmt: skip
    for address, assoc, zone, third in zip(SUBNETS, ASSOCS, ZONES, range(3), strict=True):
        out[address] = (
            {"cidr_block": f"10.43.{third}.0/24", "availability_zone": zone,
             "map_public_ip_on_launch": False}, {"id": True, "vpc_id": True},
        )  # fmt: skip
        out[assoc] = ({}, {"id": True, "subnet_id": True, "route_table_id": True})
    return out


def refs(*names: str) -> dict[str, Any]:
    return {"references": list(names)}


def configuration() -> dict[str, Any]:
    vpc = refs("aws_vpc.redshift.id", "aws_vpc.redshift")
    sg = refs("aws_security_group.redshift.id", "aws_security_group.redshift")
    slr = ["aws_iam_service_linked_role.redshift"]

    def res(address: str, **expressions: Any) -> dict[str, Any]:
        return {"address": address, "expressions": expressions}

    return {"root_module": {"module_calls": {}, "resources": [
        res("aws_vpc.redshift"),
        res("aws_default_security_group.redshift", vpc_id=vpc),
        res("aws_subnet.private", vpc_id=vpc),
        res("aws_route_table.private", vpc_id=vpc),
        {**res("aws_route_table_association.private",
               subnet_id=refs("each.value.id", "each.value"),
               route_table_id=refs("aws_route_table.private.id", "aws_route_table.private")),
         "for_each_expression": refs("aws_subnet.private")},
        res("aws_security_group.redshift", vpc_id=vpc),
        res("aws_iam_role.copy"), res("aws_iam_role.caller"),
        res("aws_iam_role_policy.copy", role=refs("aws_iam_role.copy.id", "aws_iam_role.copy")),
        res("aws_iam_role_policy.caller",
            role=refs("aws_iam_role.caller.id", "aws_iam_role.caller")),
        res("aws_iam_service_linked_role.redshift"),
        {**res(
            "aws_redshiftserverless_namespace.this",
            iam_roles=refs("aws_iam_role.copy.arn", "aws_iam_role.copy"),
        ), "depends_on": slr},
        {**res(
            "aws_redshiftserverless_workgroup.this",
            namespace_name=refs(
                "aws_redshiftserverless_namespace.this.namespace_name",
                "aws_redshiftserverless_namespace.this",
            ),
            subnet_ids=refs("aws_subnet.private"), security_group_ids=sg,
        ), "depends_on": slr},
        res("aws_redshiftserverless_usage_limit.this",
            resource_arn=refs("aws_redshiftserverless_workgroup.this.arn",
                              "aws_redshiftserverless_workgroup.this")),
    ]}}  # fmt: skip


def plan() -> dict[str, Any]:
    built = resources()
    return {
        "format_version": "1.2", "terraform_version": "1.15.8", "errored": False,
        "complete": True,
        "variables": {"region": {"value": REGION}, "expected_account_id": {"value": MEMBER},
                      "owner": {"value": "o"}},
        "prior_state": {"values": {"root_module": {"resources": [
            {"address": "data.aws_caller_identity.current", "mode": "data",
             "values": {"account_id": MEMBER}},
            {"address": "data.aws_organizations_organization.this", "mode": "data",
             "values": {"master_account_id": MANAGEMENT}},
            {"address": "data.aws_availability_zones.available", "mode": "data",
             "values": {"names": ZONES, "state": "available"}},
        ]}}},
        "resource_changes": [rc(a, *built[a]) for a in ADDRESSES],
        "output_changes": {},
        "configuration": configuration(),
    }  # fmt: skip


def change(p: dict[str, Any], address: str) -> dict[str, Any]:
    return next(r for r in p["resource_changes"] if r["address"] == address)["change"]


def config(p: dict[str, Any], address: str) -> dict[str, Any]:
    return next(
        r for r in p["configuration"]["root_module"]["resources"] if r["address"] == address
    )


@pytest.fixture
def stack(tmp_path: Path) -> Path:
    """A copy of the stack's .tf files (never a tfvars file)."""
    copy_dir = tmp_path / "redshift"
    copy_dir.mkdir()
    for tf in rpc.STACK.glob("*.tf"):
        (copy_dir / tf.name).write_text(tf.read_text())
    return copy_dir


def run(p: dict[str, Any], stack: Path) -> list[str]:
    problems, _ = rpc.check(p, stack)
    return problems


# --- the reviewed shape passes -----------------------------------------------------------------


def test_the_reviewed_plan_passes(stack: Path) -> None:
    problems, report = rpc.check(plan(), stack)
    assert problems == []
    assert "summary: 18 to add, 0 to change, 0 to destroy" in report


def test_the_report_never_prints_the_account(stack: Path) -> None:
    _, report = rpc.check(plan(), stack)
    assert MEMBER not in "\n".join(report)


# --- the resources: exactly these, only creates ------------------------------------------------


@pytest.mark.parametrize("address", ADDRESSES)
def test_a_missing_create_is_refused(stack: Path, address: str) -> None:
    p = plan()
    p["resource_changes"] = [r for r in p["resource_changes"] if r["address"] != address]
    assert any(f"missing change: create {address}" in x for x in run(p, stack))


@pytest.mark.parametrize(
    "type_",
    [
        "aws_internet_gateway", "aws_egress_only_internet_gateway", "aws_nat_gateway", "aws_eip",
        "aws_route", "aws_vpc_endpoint", "aws_vpc_security_group_ingress_rule",
        "aws_vpc_security_group_egress_rule", "aws_security_group_rule", "aws_network_acl",
        "aws_redshiftserverless_endpoint_access", "aws_redshiftserverless_snapshot",
        "aws_redshiftserverless_resource_policy", "aws_redshiftserverless_custom_domain",
        "aws_redshift_cluster", "aws_secretsmanager_secret", "aws_kms_key", "aws_s3_bucket",
        "aws_iam_user", "aws_iam_role_policy_attachment", "aws_glue_job", "aws_lambda_function",
        "aws_cloudwatch_log_group", "aws_sns_topic",
    ],
)  # fmt: skip
def test_any_other_resource_is_refused(stack: Path, type_: str) -> None:
    p = plan()
    p["resource_changes"].append(rc(f"{type_}.x", {}, {}))
    assert any(f"unexpected change: create {type_}.x" in x for x in run(p, stack))


def test_a_fourth_subnet_is_refused(stack: Path) -> None:
    p = plan()
    p["resource_changes"].append(rc('aws_subnet.private["d"]', {}, {}))
    assert any('unexpected change: create aws_subnet.private["d"]' in x for x in run(p, stack))


@pytest.mark.parametrize("actions", [["update"], ["delete"], ["delete", "create"], ["no-op"]])
def test_only_creates_are_accepted(stack: Path, actions: list[str]) -> None:
    p = plan()
    change(p, WG)["actions"] = actions
    assert run(p, stack) != []


def test_an_import_is_refused(stack: Path) -> None:
    p = plan()
    change(p, COPY)["importing"] = {"id": "ecp-redshift-copy"}
    assert run(p, stack) != []


def test_a_state_that_is_not_empty_is_refused(stack: Path) -> None:
    p = plan()
    p["prior_state"]["values"]["root_module"]["resources"].append(
        {"address": "aws_vpc.old", "mode": "managed", "values": {}}
    )
    assert any("state is not empty" in x for x in run(p, stack))


# --- the capacities, the limit and the privacy of the workgroup --------------------------------


@pytest.mark.parametrize(
    ("address", "attribute", "value"),
    [
        (WG, "base_capacity", 4), (WG, "base_capacity", 16), (WG, "base_capacity", 128),
        (WG, "base_capacity", None), (WG, "max_capacity", 4), (WG, "max_capacity", 16),
        (WG, "max_capacity", None), (WG, "max_capacity", -1),
        (WG, "publicly_accessible", True), (WG, "publicly_accessible", None),
        (WG, "enhanced_vpc_routing", True), (WG, "enhanced_vpc_routing", None),
        (WG, "workgroup_name", "other"), (WG, "namespace_name", "other"),
        (WG, "tags_all", {**TAGS, "stack": "demo"}), (WG, "tags_all", {}),
        (LIMIT, "usage_type", "cross-region-datasharing"), (LIMIT, "period", "weekly"),
        (LIMIT, "period", "monthly"), (LIMIT, "amount", 3), (LIMIT, "amount", 40),
        (LIMIT, "amount", None), (LIMIT, "breach_action", "log"),
        (LIMIT, "breach_action", "emit-metric"), (LIMIT, "breach_action", None),
        (NS, "namespace_name", "other"), (NS, "db_name", "dev"),
        (VPC, "cidr_block", "10.0.0.0/16"), (VPC, "enable_dns_support", False),
        (VPC, "enable_dns_hostnames", False),
        (SLR, "aws_service_name", "ecs.amazonaws.com"),
        (SG, "name", "default"),
    ],
)  # fmt: skip
def test_a_setting_that_differs_is_refused(
    stack: Path, address: str, attribute: str, value: Any
) -> None:
    p = plan()
    change(p, address)["after"][attribute] = value
    change(p, address)["after_unknown"].pop(attribute, None)
    assert any(address in x and attribute in x for x in run(p, stack)), attribute


# --- IAM only: no admin credential of any kind -------------------------------------------------


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("admin_user_password", "hunter2"), ("admin_user_password_wo", "hunter2"),
        ("admin_user_password_wo_version", 1), ("manage_admin_password", True),
        ("admin_password_secret_kms_key_id", "alias/x"), ("admin_username", "admin"),
        ("default_iam_role_arn", f"arn:aws:iam::{MEMBER}:role/ecp-redshift-copy"),
        ("log_exports", ["userlog"]), ("kms_key_id", "alias/x"),
    ],
)  # fmt: skip
def test_a_namespace_admin_or_key_attribute_is_refused(
    stack: Path, attribute: str, value: Any
) -> None:
    p = plan()
    change(p, NS)["after"][attribute] = value
    change(p, NS)["after_unknown"].pop(attribute, None)
    assert any(NS in x and attribute in x for x in run(p, stack)), attribute


def test_the_service_may_choose_the_admin_name_but_not_a_credential(stack: Path) -> None:
    p = plan()  # admin_username is null/unknown in the reviewed plan: computed by the service
    assert change(p, NS)["after_unknown"]["admin_username"] is True
    assert run(p, stack) == []


def test_a_known_non_secret_attribute_in_after_unknown_cannot_hide_a_credential(
    stack: Path,
) -> None:
    p = plan()
    change(p, NS)["after"]["admin_user_password"] = "a-secret"  # noqa: S105
    change(p, NS)["after_unknown"]["admin_user_password"] = True  # unknown AND set: still refused
    assert any("admin_user_password" in x for x in run(p, stack))


# --- the network: three private subnets in three zones, nothing that leads out -----------------


@pytest.mark.parametrize("index", range(3))
def test_each_subnet_is_the_reviewed_one(stack: Path, index: int) -> None:
    for attribute, value in (("cidr_block", "10.43.9.0/24"), ("map_public_ip_on_launch", True),
                             ("availability_zone", "us-east-1a")):  # fmt: skip
        p = plan()
        change(p, SUBNETS[index])["after"][attribute] = value
        assert any(SUBNETS[index] in x and attribute in x for x in run(p, stack)), attribute


def test_two_subnets_in_one_zone_are_refused(stack: Path) -> None:
    p = plan()
    change(p, SUBNETS[1])["after"]["availability_zone"] = ZONES[0]
    assert any("three different Availability Zones" in x for x in run(p, stack))


def test_the_zones_must_be_the_ones_the_account_reports(stack: Path) -> None:
    p = plan()
    p["prior_state"]["values"]["root_module"]["resources"][2]["values"]["names"] = [
        "ca-central-1a", "ca-central-1b", "ca-central-1c",
    ]  # fmt: skip
    assert any("Availability Zones the account reports" in x for x in run(p, stack))


def test_the_route_table_may_hold_no_route(stack: Path) -> None:
    p = plan()
    change(p, RT)["after"]["route"] = [{"cidr_block": "0.0.0.0/0", "gateway_id": "igw-1"}]
    change(p, RT)["after_unknown"].pop("route")
    assert any(RT in x and "route" in x for x in run(p, stack))


@pytest.mark.parametrize("address", [SG, DSG])
def test_a_security_group_with_a_rule_is_refused(stack: Path, address: str) -> None:
    for attribute in ("ingress", "egress"):
        p = plan()
        change(p, address)["after"][attribute] = [{"from_port": 5439, "cidr_blocks": ["0.0.0.0/0"]}]
        change(p, address)["after_unknown"].pop(attribute)
        assert any(address in x and attribute in x for x in run(p, stack)), attribute


@pytest.mark.parametrize("address", [SG, DSG])
def test_an_empty_rule_list_or_an_unknown_one_is_accepted(stack: Path, address: str) -> None:
    p = plan()
    for attribute in ("ingress", "egress"):
        change(p, address)["after"][attribute] = []
        change(p, address)["after_unknown"].pop(attribute)
    assert run(p, stack) == []


# --- the roles: the boundary and the reviewed templates ----------------------------------------


@pytest.mark.parametrize("address", [COPY, CALLER])
def test_a_role_without_the_boundary_is_refused(stack: Path, address: str) -> None:
    for value in (None, "arn:aws:iam::333333333333:policy/other"):
        p = plan()
        change(p, address)["after"]["permissions_boundary"] = value
        assert any(address in x and "permissions_boundary" in x for x in run(p, stack))


@pytest.mark.parametrize("address", [COPY, CALLER])
def test_a_trust_other_than_the_template_is_refused(stack: Path, address: str) -> None:
    p = plan()
    doc = json.loads(change(p, address)["after"]["assume_role_policy"])
    doc["Statement"][0]["Principal"] = {"AWS": "*"}
    change(p, address)["after"]["assume_role_policy"] = json.dumps(doc)
    assert any(address in x and "template" in x for x in run(p, stack))


@pytest.mark.parametrize("address", [COPY_POLICY, CALLER_POLICY])
def test_a_policy_other_than_the_template_is_refused(stack: Path, address: str) -> None:
    p = plan()
    doc = json.loads(change(p, address)["after"]["policy"])
    doc["Statement"].append({"Effect": "Allow", "Action": "s3:*", "Resource": "*"})
    change(p, address)["after"]["policy"] = json.dumps(doc)
    assert any(address in x and "template" in x for x in run(p, stack))


@pytest.mark.parametrize("address", [COPY, CALLER, COPY_POLICY, CALLER_POLICY])
def test_a_document_unknown_at_plan_time_is_refused(stack: Path, address: str) -> None:
    p = plan()
    attribute = "assume_role_policy" if address in (COPY, CALLER) else "policy"
    change(p, address)["after"].pop(attribute)
    change(p, address)["after_unknown"][attribute] = True
    assert any(address in x and "known at plan time" in x for x in run(p, stack))


# --- the wiring: an exact value, or an exact reference to this stack's own resource -------------

WIRING = [
    # (address, attribute, a known wrong value, the references that are the stack's own)
    (SUBNETS[0], "vpc_id", "vpc-0other", "aws_subnet.private"),
    (RT, "vpc_id", "vpc-0other", RT),
    (SG, "vpc_id", "vpc-0other", SG),
    (DSG, "vpc_id", "vpc-0other", DSG),
    (ASSOCS[0], "subnet_id", "subnet-0other", "aws_route_table_association.private"),
    (ASSOCS[0], "route_table_id", "rtb-0other", "aws_route_table_association.private"),
    (COPY_POLICY, "role", "ecp-glue-shape", COPY_POLICY),
    (CALLER_POLICY, "role", "ecp-glue-shape", CALLER_POLICY),
    (NS, "iam_roles", [f"arn:aws:iam::{MEMBER}:role/ecp-glue-shape"], NS),
    (WG, "namespace_name", "other", WG),
    (WG, "subnet_ids", ["subnet-0other"], WG),
    (WG, "security_group_ids", ["sg-0other"], WG),
    (LIMIT, "resource_arn", f"arn:aws:redshift-serverless:{REGION}:{MEMBER}:workgroup/x", LIMIT),
]  # fmt: skip


def config_address(address: str) -> str:
    return address.split("[")[0]


@pytest.mark.parametrize(("address", "attribute", "wrong", "_"), WIRING)
def test_a_known_wiring_value_that_is_not_the_reviewed_one_is_refused(
    stack: Path, address: str, attribute: str, wrong: Any, _: str
) -> None:
    p = plan()
    change(p, address)["after"][attribute] = wrong
    change(p, address)["after_unknown"].pop(attribute, None)
    assert any(address in x and attribute in x for x in run(p, stack)), attribute


@pytest.mark.parametrize(("address", "attribute", "_wrong", "_"), WIRING)
def test_an_unknown_wiring_value_from_a_foreign_reference_is_refused(
    stack: Path, address: str, attribute: str, _wrong: Any, _: str
) -> None:
    for foreign in (["aws_iam_role.other.id"], ["var.role"], ["aws_subnet.other"], []):
        p = plan()
        change(p, address)["after_unknown"][attribute] = True
        change(p, address)["after"].pop(attribute, None)
        config(p, config_address(address))["expressions"][attribute] = {"references": foreign}
        assert any(address in x and attribute in x for x in run(p, stack)), (attribute, foreign)


@pytest.mark.parametrize(("address", "attribute", "_wrong", "_"), WIRING)
def test_an_unknown_wiring_value_with_no_expression_at_all_is_refused(
    stack: Path, address: str, attribute: str, _wrong: Any, _: str
) -> None:
    p = plan()
    change(p, address)["after_unknown"][attribute] = True
    change(p, address)["after"].pop(attribute, None)
    config(p, config_address(address))["expressions"].pop(attribute, None)
    assert any(address in x and attribute in x for x in run(p, stack))


@pytest.mark.parametrize("address", ["aws_redshiftserverless_namespace.this",
                                     "aws_redshiftserverless_workgroup.this"])  # fmt: skip
def test_the_warehouse_must_wait_for_the_service_linked_role(stack: Path, address: str) -> None:
    p = plan()
    config(p, address)["depends_on"] = []
    assert any(address in x and "service-linked" in x for x in run(p, stack))


def test_the_association_must_follow_exactly_the_subnets(stack: Path) -> None:
    p = plan()
    config(p, "aws_route_table_association.private")["for_each_expression"] = {
        "references": ["aws_subnet.other"]
    }
    assert any("aws_route_table_association" in x and "for_each" in x for x in run(p, stack))


# --- the plan as a whole -----------------------------------------------------------------------


def test_an_errored_incomplete_or_drifting_plan_is_refused(stack: Path) -> None:
    for key, value in (("errored", True), ("complete", False),
                       ("resource_drift", [{"address": "aws_vpc.redshift"}]),
                       ("deferred_changes", [{"address": "aws_vpc.redshift"}])):  # fmt: skip
        p = plan()
        p[key] = value
        assert run(p, stack) != [], key


def test_an_output_change_is_refused(stack: Path) -> None:
    p = plan()
    p["output_changes"] = {"endpoint": {"actions": ["create"]}}
    assert any("output change" in x for x in run(p, stack))


def test_the_wrong_region_or_variable_is_refused(stack: Path) -> None:
    p = plan()
    p["variables"]["region"]["value"] = "us-east-1"
    p["variables"]["extra"] = {"value": "x"}
    problems = run(p, stack)
    assert any("region" in x for x in problems) and any(
        "unexpected variable" in x for x in problems
    )


def test_the_management_account_is_refused(stack: Path) -> None:
    p = plan()
    p["prior_state"]["values"]["root_module"]["resources"][1]["values"]["master_account_id"] = (
        MEMBER
    )
    assert any("management account" in x for x in run(p, stack))


def test_a_caller_that_is_not_the_expected_account_is_refused(stack: Path) -> None:
    p = plan()
    p["variables"]["expected_account_id"]["value"] = "999999999999"
    assert any("expected_account_id" in x for x in run(p, stack))


def test_any_module_call_is_refused(stack: Path) -> None:
    p = plan()
    p["configuration"]["root_module"]["module_calls"] = {"bucket": {"source": "x"}}
    assert any("module call not allowed" in x for x in run(p, stack))


# --- the source the plan was made from ---------------------------------------------------------


def test_an_override_file_is_refused(stack: Path) -> None:
    (stack / "override.tf").write_text("")
    assert any("override" in x for x in run(plan(), stack))


def test_json_configuration_is_refused(stack: Path) -> None:
    (stack / "extra.tf.json").write_text("{}")
    assert any("JSON configuration" in x for x in run(plan(), stack))


def test_a_module_block_in_the_source_is_refused(stack: Path) -> None:
    (stack / "extra.tf").write_text('module "m" {\n  source = "x"\n}\n')
    assert any("module block not allowed" in x for x in run(plan(), stack))


# --- the command line --------------------------------------------------------------------------


def test_main_prints_ok_and_exits_zero_on_the_reviewed_plan(
    stack: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan()))
    assert rpc.main(["--stack-dir", str(stack), str(path)]) == 0
    out = capsys.readouterr().out
    assert "OK    the reviewed shape" in out and MEMBER not in out


def test_main_exits_one_and_prints_the_failures(
    stack: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = copy.deepcopy(plan())
    change(p, WG)["after"]["base_capacity"] = 128
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(p))
    assert rpc.main(["--stack-dir", str(stack), str(path)]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "base_capacity" in out and "OK    the reviewed" not in out


# --- the policy gate on the same plan (conftest and the Rego policies) --------------------------


@pytest.fixture
def gate(tmp_path: Path):  # type: ignore[no-untyped-def]
    import shutil

    if shutil.which("conftest") is None:
        pytest.skip("conftest is not installed")
    import policy_gate

    def run_gate(p: dict[str, Any]) -> tuple[int, str]:
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(p))
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = policy_gate.main(
                ["policy_gate.py", str(path), str(ROOT / "policy"), "--stack", "redshift",
                 "--stack-dir", str(ROOT / "infra" / "redshift")]
            )  # fmt: skip
        return code, out.getvalue()

    return run_gate


def test_the_policy_gate_passes_the_reviewed_plan(gate) -> None:  # type: ignore[no-untyped-def]
    code, out = gate(plan())
    assert code == 0, out
    assert "18 resources" in out and "0 failures" in out


def test_the_policy_gate_refuses_the_default_capacity(gate) -> None:  # type: ignore[no-untyped-def]
    p = plan()
    change(p, WG)["after"]["base_capacity"] = None
    code, out = gate(p)
    assert code == 1 and "base_capacity must be set" in out


def test_the_policy_gate_refuses_an_internet_gateway_and_an_admin_password(gate) -> None:  # type: ignore[no-untyped-def]
    p = plan()
    p["resource_changes"].append(rc("aws_internet_gateway.x", {}, {}))
    change(p, NS)["after"]["admin_user_password"] = "a-secret"  # noqa: S105
    code, out = gate(p)
    assert code == 1 and "no gateway, internet route" in out and "IAM only" in out


def test_the_policy_gate_refuses_a_role_without_the_boundary_or_trusting_everyone(gate) -> None:  # type: ignore[no-untyped-def]
    p = plan()
    change(p, COPY)["after"]["permissions_boundary"] = None
    doc = json.loads(change(p, CALLER)["after"]["assume_role_policy"])
    doc["Statement"][0]["Principal"] = "*"
    change(p, CALLER)["after"]["assume_role_policy"] = json.dumps(doc)
    code, out = gate(p)
    assert code == 1 and "permissions_boundary" in out and "any principal" in out
