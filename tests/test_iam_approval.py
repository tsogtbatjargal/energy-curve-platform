"""PLAN.md R1 gate (ADR-0017), against a real plan of the fixture stack.

`tests/fixtures/iam_gate/fixture-plan.json` is real `terraform show -json` output (Terraform
1.15.8, hashicorp/aws ~> 6.67) of `tests/fixtures/iam_gate/stack`, planned offline with dummy
credentials. Regenerate it after editing the stack (the opt-in test below checks it):

    cd tests/fixtures/iam_gate/stack && terraform init && terraform plan -out=/tmp/fx.tfplan
    terraform show -json /tmp/fx.tfplan > ../fixture-plan.json
    cp .terraform/modules/modules.json ../fixture-modules.json

`registry/cloudposse-label-0.25.0` holds the `*.tf` files and LICENSE (Apache-2.0) of the
registry module as `terraform init` downloads it (tag 0.25.0, commit 488ab91e), so the gate reads
real third-party locals.

Each case changes one input and must fail the gate, unless it says it passes.
"""

import copy
import json
import os
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import iam_approval as ia
import policy_gate
import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "iam_gate"
TODAY = date(2026, 10, 3)
POLICIES = ("aws_iam_policy.logs", "aws_iam_policy.write", "aws_iam_role_policy.read")


@pytest.fixture
def plan() -> dict[str, Any]:
    return json.loads((FIXTURE / "fixture-plan.json").read_text())


@pytest.fixture
def stack(tmp_path: Path) -> Path:
    """The fixture stack as `terraform init` leaves it: modules.json and downloaded modules."""
    d = tmp_path / "stack"
    shutil.copytree(FIXTURE / "stack", d, ignore=shutil.ignore_patterns(".terraform"))
    modules = d / ".terraform" / "modules"
    shutil.copytree(FIXTURE / "registry" / "cloudposse-label-0.25.0", modules / "label")
    shutil.copy(FIXTURE / "fixture-modules.json", modules / "modules.json")
    return d


def approval(plan: dict, stack_dir: Path, of: str, **over: Any) -> ia.Approval:
    """An approval of `of`'s current fingerprint; `over` replaces fields (stack, address...)."""
    fields = {
        "stack": "batch", "address": of, "attribute": "policy",
        "fingerprint": ia.fingerprint(plan, "batch", stack_dir, of),
        "reviewer": "reviewer", "approved_on": TODAY - timedelta(days=1),
        "expires_on": TODAY + timedelta(days=29), "reason": "fixture",
    }  # fmt: skip
    return ia.Approval(**{**fields, **over})


def approve_all(plan: dict, stack: Path) -> list[ia.Approval]:
    return [approval(plan, stack, a) for a in POLICIES]


def gate(plan: dict, stack: Path, approvals: list[ia.Approval]) -> ia.Result:
    return ia.check(plan, "batch", stack, approvals, today=TODAY)


def failing(result: ia.Result) -> set[str]:
    return {f.split(":")[1].rsplit(".", 1)[0] for f in result.failures}


def test_the_fixture_is_a_real_plan_with_three_unknown_policies(plan: dict) -> None:
    assert plan["terraform_version"] == "1.15.8"
    assert [a for a, _, _ in ia.unknown_policies(plan)] == sorted(POLICIES)
    expressions = {
        r["address"]: r["expressions"]["policy"]
        for r in plan["configuration"]["root_module"]["resources"]
        if r["address"] in POLICIES
    }
    # The facts R1 rests on: jsonencode literals and data-source statements are not in it.
    assert expressions["aws_iam_policy.write"] == {
        "references": ["aws_s3_bucket.data.arn", "aws_s3_bucket.data"]
    }
    assert expressions["aws_iam_role_policy.read"]["references"][0] == (
        "data.aws_iam_policy_document.read.json"
    )


def test_an_unknown_policy_with_no_approval_fails(plan: dict, stack: Path) -> None:
    result = gate(plan, stack, [])
    assert failing(result) == set(POLICIES)
    assert all("not approved (sha256:" in f for f in result.failures)


def test_the_matching_fingerprint_passes(plan: dict, stack: Path) -> None:
    result = gate(plan, stack, approve_all(plan, stack))
    assert result.failures == [] and len(result.notes) == 3


def test_the_fingerprint_is_deterministic(plan: dict, stack: Path, tmp_path: Path) -> None:
    again = tmp_path / "again"
    shutil.copytree(stack, again)
    assert ia.fingerprint(plan, "batch", stack, POLICIES[0]) == ia.fingerprint(
        copy.deepcopy(plan), "batch", again, POLICIES[0]
    )


def root_resource(plan: dict, address: str) -> dict:
    return next(
        r for r in plan["configuration"]["root_module"]["resources"] if r["address"] == address
    )


def test_a_changed_policy_document_statement_fails(plan: dict, stack: Path) -> None:
    approvals = approve_all(plan, stack)
    doc = root_resource(plan, "data.aws_iam_policy_document.logs")
    doc["expressions"]["statement"][0]["actions"] = {"constant_value": ["logs:*"]}
    assert failing(gate(plan, stack, approvals)) == {"aws_iam_policy.logs"}


def test_a_changed_local_fails(plan: dict, stack: Path) -> None:
    approvals = approve_all(plan, stack)
    tf = stack / "main.tf"
    tf.write_text(tf.read_text().replace('"s3:ListBucket"]', '"s3:ListBucket", "s3:*"]'))
    assert "aws_iam_role_policy.read" in failing(gate(plan, stack, approvals))


def test_a_changed_literal_inside_jsonencode_fails(plan: dict, stack: Path) -> None:
    approvals = approve_all(plan, stack)
    tf = stack / "main.tf"
    tf.write_text(tf.read_text().replace('Action = ["s3:PutObject"]', 'Action = ["s3:*"]'))
    assert "aws_iam_policy.write" in failing(gate(plan, stack, approvals))


def test_a_changed_resolved_module_version_fails(plan: dict, stack: Path) -> None:
    approvals = approve_all(plan, stack)
    modules = stack / ".terraform" / "modules" / "modules.json"
    modules.write_text(modules.read_text().replace('"0.25.0"', '"0.25.1"'))
    assert failing(gate(plan, stack, approvals)) == {"aws_iam_policy.logs"}


def test_a_changed_module_source_fails(plan: dict, stack: Path) -> None:
    approvals = approve_all(plan, stack)
    plan["configuration"]["root_module"]["module_calls"]["label"]["source"] = "evil/label/null"
    assert failing(gate(plan, stack, approvals)) == {"aws_iam_policy.logs"}


def test_changed_registry_module_code_under_the_same_version_fails(plan: dict, stack: Path) -> None:
    """Beyond the PLAN text: a moved or re-published tag changes the downloaded files."""
    approvals = approve_all(plan, stack)
    (stack / ".terraform" / "modules" / "label" / "main.tf").write_text("# retagged\n")
    assert failing(gate(plan, stack, approvals)) == {"aws_iam_policy.logs"}


def test_a_changed_input_variable_value_fails(plan: dict, stack: Path) -> None:
    approvals = approve_all(plan, stack)
    plan["variables"]["suffix"]["value"] = "other"
    assert failing(gate(plan, stack, approvals)) == set(POLICIES)


def test_a_changed_provider_lock_fails(plan: dict, stack: Path) -> None:
    approvals = approve_all(plan, stack)
    lock = stack / ".terraform.lock.hcl"
    lock.write_text(lock.read_text().replace("6.", "7.", 1))
    assert failing(gate(plan, stack, approvals)) == set(POLICIES)


def test_any_edit_to_a_stack_file_fails(plan: dict, stack: Path) -> None:
    approvals = approve_all(plan, stack)
    (stack / "unrelated.tf").write_text("# an unrelated new file\n")
    assert failing(gate(plan, stack, approvals)) == set(POLICIES)  # conservative by design


def test_a_local_module_edit_fails_only_the_policies_that_reach_it(plan: dict, stack: Path) -> None:
    approvals = approve_all(plan, stack)
    module = stack / "modules" / "logs" / "main.tf"
    module.write_text(module.read_text() + "\n# edited\n")
    assert failing(gate(plan, stack, approvals)) == {"aws_iam_policy.logs"}


def test_a_sensitive_variable_in_the_closure_fails_even_with_an_approval(
    plan: dict, stack: Path
) -> None:
    plan["configuration"]["root_module"]["variables"]["suffix"]["sensitive"] = True
    result = gate(plan, stack, approve_all(plan, stack))  # approvals for this very plan
    assert failing(result) == set(POLICIES)
    assert all("sensitive variable(s) ['var.suffix']" in f for f in result.failures)


def test_an_unknown_value_that_is_not_an_arn_id_or_name_fails(plan: dict, stack: Path) -> None:
    bucket = next(r for r in plan["resource_changes"] if r["address"] == "aws_s3_bucket.data")
    assert bucket["change"]["after_unknown"]["bucket_regional_domain_name"] is True
    root_resource(plan, "aws_iam_policy.write")["expressions"]["policy"] = {
        "references": ["aws_s3_bucket.data.bucket_regional_domain_name", "aws_s3_bucket.data"]
    }
    result = gate(plan, stack, approve_all(plan, stack))
    assert failing(result) == {"aws_iam_policy.write"}
    assert "aws_s3_bucket.data.bucket_regional_domain_name" in result.failures[0]


def use_local(plan: dict, stack: Path, hcl: str, name: str, variable: dict | None = None) -> None:
    """Append `hcl` (a variable and a locals block) to main.tf and make aws_iam_policy.write
    reference `local.<name>`, as Terraform records it (verified on a real 1.15.8 plan)."""
    tf = stack / "main.tf"
    tf.write_text(tf.read_text() + "\n" + hcl)
    if variable is not None:
        plan["configuration"]["root_module"]["variables"].update(variable["config"])
        plan["variables"].update(variable["values"])
    root_resource(plan, "aws_iam_policy.write")["expressions"]["policy"]["references"].append(
        f"local.{name}"
    )


def test_a_sensitive_variable_reached_through_a_local_fails(plan: dict, stack: Path) -> None:
    hcl = 'variable "token" {\n  sensitive = true\n}\n\nlocals {\n  tag = var.token\n}\n'
    use_local(plan, stack, hcl, "tag", {"config": {"token": {"sensitive": True}},
                                        "values": {"token": {"value": "t"}}})  # fmt: skip
    result = gate(plan, stack, approve_all(plan, stack))
    assert failing(result) == {"aws_iam_policy.write"}
    assert "sensitive variable(s) ['var.token']" in result.failures[0]


def test_an_unknown_value_reached_through_a_local_must_be_an_arn_id_or_name(
    plan: dict, stack: Path
) -> None:
    hcl = "locals {\n  host = aws_s3_bucket.data.bucket_regional_domain_name\n}\n"
    use_local(plan, stack, hcl, "host")
    result = gate(plan, stack, approve_all(plan, stack))
    assert failing(result) == {"aws_iam_policy.write"}
    assert "aws_s3_bucket.data.bucket_regional_domain_name" in result.failures[0]


def test_a_variable_value_reached_through_a_local_is_fingerprinted(plan: dict, stack: Path) -> None:
    hcl = 'variable "extra" {\n  type = string\n}\n\nlocals {\n  extra = var.extra\n}\n'
    values = {"extra": {"value": "s3:GetObject"}}
    use_local(plan, stack, hcl, "extra", {"config": {"extra": {}}, "values": values})
    approvals = approve_all(plan, stack)
    plan["variables"]["extra"]["value"] = "s3:*"  # a tfvars change: no source file changes
    assert failing(gate(plan, stack, approvals)) == {"aws_iam_policy.write"}


def test_a_local_missing_from_the_source_fails(plan: dict, stack: Path) -> None:
    approvals = approve_all(plan, stack)
    root_resource(plan, "aws_iam_policy.write")["expressions"]["policy"]["references"].append(
        "local.gone"
    )
    result = gate(plan, stack, approvals)
    assert failing(result) == {"aws_iam_policy.write"}
    assert "local.gone: not found in the source" in result.failures[0]


def test_local_references_are_read_from_the_source_text(tmp_path: Path) -> None:
    (tmp_path / "locals.tf").write_text(
        "locals {\n"
        '  tag  = "x-${var.token}-${lookup(local.m, "k", "}")}"\n'
        "  m    = { a = module.label.id, b = data.aws_caller_identity.me.account_id }\n"
        "  doc  = <<-EOT\n    ${aws_s3_bucket.data[0].arn}\n  EOT\n"
        "  all  = aws_s3_bucket.data[*].id # var.in_a_comment\n"
        '  list = ["s3:GetObject"]\n'
        "}\n"
    )  # fmt: skip
    found = ia.Walker({"configuration": {"root_module": {}}}, tmp_path).locals_in(())

    def refs(name: str) -> set[str]:
        return {ia.strip_keys(m.group()) for m in ia.TRAVERSAL.finditer(found[name])}

    assert refs("tag") == {"var.token", "local.m"}
    assert refs("m") == {"module.label.id", "data.aws_caller_identity.me.account_id"}
    assert refs("doc") == {"aws_s3_bucket.data.arn"}
    assert refs("all") == {"aws_s3_bucket.data.id"}  # comments are not expressions
    assert refs("list") == set()


def test_a_policy_marked_sensitive_by_terraform_fails(plan: dict, stack: Path) -> None:
    """Terraform propagates sensitivity marks into unknown values (verified on 1.15.8):
    e.g. a local wrapping a literal in sensitive(), which names no variable."""
    rc = next(r for r in plan["resource_changes"] if r["address"] == "aws_iam_policy.write")
    rc["change"]["after_sensitive"] = {"policy": True}
    result = gate(plan, stack, approve_all(plan, stack))
    assert failing(result) == {"aws_iam_policy.write"}
    assert "marked sensitive" in result.failures[0]


INNER = "module.logs.aws_iam_policy.inner"


def test_a_policy_inside_a_module_hashes_that_module(plan: dict, stack: Path) -> None:
    """The policy's own module is in its closure even when nothing references the module."""
    module = stack / "modules" / "logs" / "main.tf"
    module.write_text(module.read_text() + (
        '\nresource "aws_sns_topic" "inner" {\n  name = "inner"\n}\n'
        '\nresource "aws_iam_policy" "inner" {\n  name   = "inner"\n  policy = jsonencode({'
        ' Statement = [{ Effect = "Allow", Action = ["sns:Publish"],'
        " Resource = aws_sns_topic.inner.arn }] })\n}\n"
    ))  # fmt: skip
    logs = plan["configuration"]["root_module"]["module_calls"]["logs"]["module"]
    logs["resources"] += [{
        "address": "aws_sns_topic.inner", "mode": "managed", "type": "aws_sns_topic",
        "name": "inner", "provider_config_key": "aws", "schema_version": 0,
        "expressions": {"name": {"constant_value": "inner"}},
    }, {
        "address": "aws_iam_policy.inner", "mode": "managed", "type": "aws_iam_policy",
        "name": "inner", "provider_config_key": "aws", "schema_version": 0,
        "expressions": {"policy": {"references": ["aws_sns_topic.inner.arn",
                                                  "aws_sns_topic.inner"]}},
    }]  # fmt: skip
    plan["resource_changes"] += [{
        "address": f"module.logs.aws_{kind}.inner", "module_address": "module.logs",
        "mode": "managed", "type": f"aws_{kind}", "name": "inner",
        "change": {"actions": ["create"], "after_unknown": {attr: True}},
    } for kind, attr in (("sns_topic", "arn"), ("iam_policy", "policy"))]  # fmt: skip
    approvals = [*approve_all(plan, stack), approval(plan, stack, INNER)]
    assert gate(plan, stack, approvals).failures == []
    module.write_text(module.read_text().replace('["sns:Publish"]', '["sns:*"]'))
    assert failing(gate(plan, stack, approvals)) == {"aws_iam_policy.logs", INNER}


def test_an_unknown_value_from_another_data_source_fails(plan: dict, stack: Path) -> None:
    """Not a resource in this stack: e.g. an identity looked up at apply time."""
    root = plan["configuration"]["root_module"]
    root["resources"].append(
        {
            "address": "data.aws_caller_identity.me",
            "mode": "data",
            "type": "aws_caller_identity",
            "name": "me",
            "expressions": {},
        }
    )
    plan["resource_changes"].append(
        {
            "address": "data.aws_caller_identity.me",
            "mode": "data",
            "type": "aws_caller_identity",
            "change": {"actions": ["read"], "after_unknown": {"account_id": True}},
        }
    )
    doc = root_resource(plan, "data.aws_iam_policy_document.logs")
    doc["expressions"]["statement"][0]["resources"] = {
        "references": ["data.aws_caller_identity.me.account_id", "data.aws_caller_identity.me"]
    }  # fmt: skip
    result = gate(plan, stack, approve_all(plan, stack))
    assert failing(result) == {"aws_iam_policy.logs"}
    assert "data.aws_caller_identity.me.account_id" in result.failures[0]


def test_an_expired_approval_fails(plan: dict, stack: Path) -> None:
    approvals = approve_all(plan, stack)
    stale = [approval(plan, stack, POLICIES[0], approved_on=TODAY - timedelta(days=31),
                      expires_on=TODAY - timedelta(days=1)), *approvals[1:]]  # fmt: skip
    result = gate(plan, stack, stale)
    assert failing(result) == {POLICIES[0]} and "expired on" in result.failures[0]


@pytest.mark.parametrize("over", [{"stack": "demo"}, {"address": "aws_iam_policy.other"}])
def test_an_approval_for_another_stack_or_address_fails(
    plan: dict, stack: Path, over: dict
) -> None:
    approvals = [approval(plan, stack, POLICIES[0], **over), *approve_all(plan, stack)[1:]]
    assert failing(gate(plan, stack, approvals)) == {POLICIES[0]}


def test_the_bootstrap_stack_only_notes_unknown_policies(plan: dict, stack: Path) -> None:
    result = ia.check(plan, "bootstrap", stack, [], today=TODAY)
    assert result.failures == [] and len(result.notes) == 3


def test_resources_being_deleted_are_not_checked(plan: dict, stack: Path) -> None:
    for rc in plan["resource_changes"]:
        rc["change"]["actions"] = ["delete"]
    assert gate(plan, stack, []).failures == []


def test_a_missing_lock_file_or_module_download_fails(plan: dict, stack: Path) -> None:
    shutil.rmtree(stack / ".terraform" / "modules" / "label")
    assert "not downloaded" in " ".join(gate(plan, stack, []).failures)
    (stack / ".terraform.lock.hcl").unlink()
    assert len(gate(plan, stack, []).failures) == 3


# --- the approvals file ---------------------------------------------------------------------------


def write(path: Path, entries: list[dict]) -> Path:
    path.write_text(json.dumps({"approvals": entries}))
    return path


def entry(**over: Any) -> dict:
    return {"stack": "batch", "address": "aws_iam_policy.write", "attribute": "policy",
            "fingerprint": "sha256:" + "a" * 64, "reviewer": "r", "approved_on": "2026-10-01",
            "expires_on": "2026-10-31", "reason": "x", **over}  # fmt: skip


def test_a_valid_approvals_file_loads(tmp_path: Path) -> None:
    (loaded,) = ia.load_approvals(write(tmp_path / "a.json", [entry()]))
    assert loaded.expires_on == date(2026, 10, 31)
    assert ia.load_approvals(tmp_path / "missing.json") == []


@pytest.mark.parametrize(
    ("entries", "why"),
    [
        ([entry(expires_on="2026-11-01")], "0-30 days"),  # 31 days after approval
        ([entry(expires_on="2026-09-30")], "0-30 days"),  # before approval
        ([entry(fingerprint="abc")], "sha256:"),
        ([entry(reviewer="")], "non-empty"),
        ([{k: v for k, v in entry().items() if k != "reason"}], "fields must be exactly"),
        ([entry(extra="x")], "fields must be exactly"),
        ([entry(approved_on="1 Oct")], "YYYY-MM-DD"),
        ([entry(), entry()], "duplicate"),
    ],
)
def test_an_invalid_approvals_file_fails_the_gate(
    tmp_path: Path, entries: list[dict], why: str
) -> None:
    with pytest.raises(ia.GateError, match=why):
        ia.load_approvals(write(tmp_path / "a.json", entries))


def test_the_committed_approvals_file_is_valid() -> None:
    path = Path(__file__).parents[1] / "policy" / "approvals" / "iam_unknown.json"
    assert isinstance(ia.load_approvals(path), list)


# --- through policy_gate.py -----------------------------------------------------------------------


def test_policy_gate_fails_a_workload_stack_and_passes_it_when_approved(
    plan: dict, stack: Path, tmp_path: Path
) -> None:
    code, lines = policy_gate.iam_check(plan, "batch", stack, tmp_path / "none.json")
    assert code == 1 and sum(line.startswith("FAIL  R1:") for line in lines) == 3
    today = date.today()  # iam_check uses the real clock
    approvals = [
        {**a.__dict__, "approved_on": (today - timedelta(days=1)).isoformat(),
         "expires_on": (today + timedelta(days=1)).isoformat()}
        for a in approve_all(plan, stack)
    ]  # fmt: skip
    code, lines = policy_gate.iam_check(plan, "batch", stack, write(tmp_path / "a.json", approvals))
    assert code == 0 and sum(line.startswith("NOTE  R1:") for line in lines) == 3


def test_policy_gate_requires_the_stack(tmp_path: Path) -> None:
    assert policy_gate.main(["policy_gate.py", "plan.json", "policy"]) == 2


def test_the_cli_prints_fingerprints_and_never_plan_values(
    plan: dict, stack: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_path = tmp_path / "p.json"
    plan_path.write_text(json.dumps(plan))
    args = [str(plan_path), "--stack", "batch", "--stack-dir", str(stack)]
    assert ia.main(["check", *args, "--approvals", str(tmp_path / "none.json")]) == 1
    out = capsys.readouterr().out
    assert out.count("FAIL") == 3 and "sha256:" in out
    assert "fixture" not in out  # the variable value never reaches the output
    assert ia.main(["fingerprint", *args, "--address", "aws_iam_policy.write"]) == 0
    assert capsys.readouterr().out.strip() == ia.fingerprint(plan, "batch", stack,
                                                             "aws_iam_policy.write")  # fmt: skip


@pytest.mark.skipif(not os.environ.get("ECP_TERRAFORM_FIXTURE"), reason="opt-in: needs terraform")
def test_the_committed_fixture_plan_matches_a_fresh_plan(tmp_path: Path) -> None:
    """ECP_TERRAFORM_FIXTURE=1: re-plan the fixture stack and compare its configuration."""
    d = FIXTURE / "stack"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    run = {"cwd": d, "env": env, "check": True, "capture_output": True}
    subprocess.run(["terraform", "init", "-input=false"], **run)  # noqa: S603, S607
    subprocess.run(["terraform", "plan", "-input=false", f"-out={tmp_path}/p"], **run)  # noqa: S603, S607
    fresh = json.loads(subprocess.run(["terraform", "show", "-json", f"{tmp_path}/p"],  # noqa: S603, S607
                                      **run).stdout)  # fmt: skip
    committed = json.loads((FIXTURE / "fixture-plan.json").read_text())
    for key in ("configuration", "variables"):
        assert fresh[key] == committed[key], key

    def unordered(p: dict) -> list[str]:  # Terraform emits these in no fixed order
        return sorted(json.dumps(x, sort_keys=True) for x in p["relevant_attributes"])

    assert unordered(fresh) == unordered(committed)
