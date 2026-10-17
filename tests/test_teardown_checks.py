"""The closing teardown of the batch and Glue stacks: `prepare` (the flag apply) and `destroy`.

Both stacks get the same two modes. `prepare` is the in-place apply that sets `force_destroy` on
the bucket (and `force_delete` on the batch ECR repository) so a destroy may empty them; `destroy`
is the teardown itself, from a state that holds those flags as true. Plans are synthetic, shaped
like `terraform show -json`; account IDs are placeholders.
"""

import copy
import json
from pathlib import Path
from typing import Any

import batch_plan_check as bpc
import glue_plan_check as gpc
import pytest
import test_batch_plan_check as tb
import test_glue_plan_check as tg

MEMBER = tb.MEMBER

BUCKET_B, ECR = f"{bpc._B}.this[0]", "aws_ecr_repository.batch"
BUCKET_G = f"{gpc._B}.this[0]"
SLR = "aws_iam_service_linked_role.ecs"


# --- the batch stack ----------------------------------------------------------------------------


def batch_applied() -> dict[str, dict[str, Any]]:
    """Address -> the applied stack's attributes (the stage-3 plan's, all unchanged)."""
    p = tb.plan(3)
    return {
        r["address"]: copy.deepcopy(r["change"]["after"] or r["change"]["before"] or {})
        for r in p["resource_changes"]
    }


def batch_plan(mode: str) -> dict[str, Any]:
    p = tb.plan(3)
    values = batch_applied()
    flags = {BUCKET_B: "force_destroy", ECR: "force_delete"}
    for r in p["resource_changes"]:
        address, c = r["address"], r["change"]
        before = copy.deepcopy(values[address])
        if mode == "prepare":
            after = copy.deepcopy(before)
            if address in flags:
                before[flags[address]], after[flags[address]] = False, True
                c.update(actions=["update"], before=before, after=after)
            else:
                c.update(actions=["no-op"], before=before, after=before)
        else:
            if address in flags:
                before[flags[address]] = True
            c.update(actions=["delete"], before=before, after=None)
    if mode == "destroy":
        p["resource_changes"] = [r for r in p["resource_changes"] if r["address"] != SLR]
    return p


def run_batch(p: dict[str, Any], mode: str, stack: Path) -> list[str]:
    problems, _ = bpc.check_teardown(p, mode, stack)
    return problems


@pytest.fixture
def bstack(tmp_path: Path) -> Path:
    d = tmp_path / "batch"
    d.mkdir()
    for tf in bpc.STACK.glob("*.tf"):
        (d / tf.name).write_text(tf.read_text())
    return d


def test_batch_prepare_accepts_exactly_the_two_flag_updates(bstack: Path) -> None:
    problems, report = bpc.check_teardown(batch_plan("prepare"), "prepare", bstack)
    assert problems == []
    assert "summary: 0 to add, 2 to change, 0 to destroy" in report
    assert MEMBER not in "\n".join(report)


def test_batch_destroy_accepts_the_43_deletes_without_the_service_linked_role(
    bstack: Path,
) -> None:
    problems, report = bpc.check_teardown(batch_plan("destroy"), "destroy", bstack)
    assert problems == []
    assert "summary: 0 to add, 0 to change, 43 to destroy" in report
    assert SLR not in "\n".join(report)


@pytest.mark.parametrize("address", [BUCKET_B, ECR])
def test_batch_prepare_refuses_a_missing_or_wrong_flag_update(bstack: Path, address: str) -> None:
    p = batch_plan("prepare")
    tb.change(p, address)["change"].update(actions=["no-op"])
    assert any(address in x for x in run_batch(p, "prepare", bstack))
    p = batch_plan("prepare")
    flag = "force_destroy" if address == BUCKET_B else "force_delete"
    tb.change(p, address)["change"]["after"][flag] = False
    assert any(address in x for x in run_batch(p, "prepare", bstack))


@pytest.mark.parametrize("address", [BUCKET_B, ECR])
def test_batch_prepare_refuses_any_other_attribute_changing_with_the_flag(
    bstack: Path, address: str
) -> None:
    p = batch_plan("prepare")
    tb.change(p, address)["change"]["after"]["name_changed_too"] = "x"
    assert any(address in x and "only" in x for x in run_batch(p, "prepare", bstack))
    p = batch_plan("prepare")
    tb.change(p, address)["change"]["after_unknown"] = {"arn": True}
    assert any(address in x for x in run_batch(p, "prepare", bstack))


@pytest.mark.parametrize("actions", [["update"], ["delete"], ["create"], ["delete", "create"]])
def test_batch_prepare_refuses_any_other_resource_changing(
    bstack: Path, actions: list[str]
) -> None:
    p = batch_plan("prepare")
    address = "aws_vpc.batch"
    tb.change(p, address)["change"]["actions"] = actions
    assert any(address in x for x in run_batch(p, "prepare", bstack))


def test_batch_prepare_refuses_a_missing_resource_and_an_import(bstack: Path) -> None:
    p = batch_plan("prepare")
    p["resource_changes"] = [r for r in p["resource_changes"] if r["address"] != "aws_vpc.batch"]
    assert any("aws_vpc.batch" in x for x in run_batch(p, "prepare", bstack))
    p = batch_plan("prepare")
    tb.change(p, "aws_vpc.batch")["change"]["importing"] = {"id": "x"}
    assert any("aws_vpc.batch" in x for x in run_batch(p, "prepare", bstack))


def test_batch_prepare_keeps_the_service_linked_role_in_the_plan(bstack: Path) -> None:
    p = batch_plan("prepare")
    p["resource_changes"] = [r for r in p["resource_changes"] if r["address"] != SLR]
    assert any(SLR in x for x in run_batch(p, "prepare", bstack))


def test_batch_destroy_refuses_the_service_linked_role_in_any_form(bstack: Path) -> None:
    for actions in (["delete"], ["forget"], ["no-op"]):
        p = batch_plan("destroy")
        p["resource_changes"].append(
            {"address": SLR, "mode": "managed", "type": "aws_iam_service_linked_role",
             "name": "ecs", "change": {"actions": actions, "before": {}, "after": None}}
        )  # fmt: skip
        assert any(SLR in x and "kept" in x for x in run_batch(p, "destroy", bstack)), actions


@pytest.mark.parametrize("address", [BUCKET_B, ECR])
def test_batch_destroy_refuses_a_flag_that_is_not_true_in_the_state(
    bstack: Path, address: str
) -> None:
    flag = "force_destroy" if address == BUCKET_B else "force_delete"
    p = batch_plan("destroy")
    tb.change(p, address)["change"]["before"][flag] = False
    assert any(address in x and flag in x for x in run_batch(p, "destroy", bstack))


def test_batch_destroy_refuses_a_missing_delete_and_any_other_stack(bstack: Path) -> None:
    p = batch_plan("destroy")
    p["resource_changes"] = [r for r in p["resource_changes"] if r["address"] != "aws_vpc.batch"]
    assert any("aws_vpc.batch" in x for x in run_batch(p, "destroy", bstack))
    for address in ("aws_glue_job.shape", "aws_redshiftserverless_workgroup.this"):
        p = batch_plan("destroy")
        p["resource_changes"].append(
            {"address": address, "mode": "managed", "type": address.split(".")[0],
             "name": "x", "change": {"actions": ["delete"], "before": {}, "after": None}}
        )  # fmt: skip
        assert any(address in x for x in run_batch(p, "destroy", bstack)), address


@pytest.mark.parametrize(
    "actions", [["no-op"], ["update"], ["create"], ["delete", "create"], ["forget"]]
)
def test_batch_destroy_accepts_deletes_only(bstack: Path, actions: list[str]) -> None:
    p = batch_plan("destroy")
    tb.change(p, "aws_vpc.batch")["change"]["actions"] = actions
    assert any("aws_vpc.batch" in x for x in run_batch(p, "destroy", bstack))


@pytest.mark.parametrize("mode", ["prepare", "destroy"])
@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(errored=True),
        lambda p: p.update(complete=False),
        lambda p: p.update(deferred_changes=[{"address": "x"}]),
        lambda p: p.update(resource_drift=[{"address": "aws_vpc.batch"}]),
        lambda p: p.update(output_changes={"o": {"actions": ["update"]}}),
        lambda p: p["variables"]["region"].update(value="us-east-1"),
        lambda p: p["variables"]["expected_account_id"].update(value="444444444444"),
    ],
)
def test_batch_teardown_modes_keep_the_soundness_checks(
    bstack: Path, mode: str, mutate: Any
) -> None:
    p = batch_plan(mode)
    mutate(p)
    assert run_batch(p, mode, bstack)


def test_batch_teardown_modes_keep_the_source_checks(bstack: Path) -> None:
    (bstack / "extra.tf").write_text('module "m" { source = "./x" }\n')
    for mode in ("prepare", "destroy"):
        assert any("module block" in x for x in run_batch(batch_plan(mode), mode, bstack))


def test_batch_unknown_modes_and_the_stage_checks_still_refuse_these_plans(
    bstack: Path,
) -> None:
    problems, _ = bpc.check_teardown(batch_plan("destroy"), "remove", bstack)
    assert any("unknown change mode" in x for x in problems)
    assert run_batch(batch_plan("destroy"), "prepare", bstack)
    assert run_batch(batch_plan("prepare"), "destroy", bstack)
    assert tb.run(batch_plan("prepare"), 4, bstack)


def test_batch_main_takes_change_instead_of_stage(
    bstack: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(batch_plan("destroy")))
    assert bpc.main(["--stack-dir", str(bstack), "--change", "destroy", str(path)]) == 0
    out = capsys.readouterr().out
    assert "OK    destroy: the reviewed shape" in out and MEMBER not in out
    assert bpc.main(["--stack-dir", str(bstack), "--change", "prepare", str(path)]) == 1
    with pytest.raises(SystemExit) as exit_info:  # neither --stage nor --change
        bpc.main(["--stack-dir", str(bstack), str(path)])
    assert exit_info.value.code == 2


# --- the Glue stack -----------------------------------------------------------------------------


def glue_plan(mode: str) -> dict[str, Any]:
    p = tg.plan()
    root = [{"address": a, "mode": "managed", "values": {}} for a in sorted(gpc.APPLIED)]
    p["prior_state"]["values"]["root_module"]["resources"] += root
    for r in p["resource_changes"]:
        address, c = r["address"], r["change"]
        before = copy.deepcopy(c["after"] or {})
        if mode == "prepare":
            if address == BUCKET_G:
                before["force_destroy"] = False
                after = copy.deepcopy(before)
                after["force_destroy"] = True
                c.update(actions=["update"], before=before, after=after)
            else:
                c.update(actions=["no-op"], before=before, after=before)
        else:
            if address == BUCKET_G:
                before["force_destroy"] = True
            c.update(actions=["delete"], before=before, after=None)
    return p


@pytest.fixture
def gstack(tmp_path: Path) -> Path:
    d = tmp_path / "glue"
    d.mkdir()
    for tf in gpc.STACK.glob("*.tf"):
        (d / tf.name).write_text(tf.read_text())
    return d


def run_glue(p: dict[str, Any], mode: str, stack: Path) -> list[str]:
    problems, _ = gpc.check(p, stack, mode)
    return problems


def test_glue_prepare_accepts_the_one_flag_update(gstack: Path) -> None:
    problems, report = gpc.check(glue_plan("prepare"), gstack, "prepare")
    assert problems == []
    assert "summary: 0 to add, 1 to change, 0 to destroy" in report


def test_glue_destroy_accepts_all_fourteen_deletes(gstack: Path) -> None:
    problems, report = gpc.check(glue_plan("destroy"), gstack, "destroy")
    assert problems == []
    assert "summary: 0 to add, 0 to change, 14 to destroy" in report
    assert MEMBER not in "\n".join(report)


def test_glue_prepare_refuses_a_wrong_or_extra_change(gstack: Path) -> None:
    p = glue_plan("prepare")
    tg.change(p, BUCKET_G)["change"]["after"]["force_destroy"] = False
    assert any(BUCKET_G in x for x in run_glue(p, "prepare", gstack))
    p = glue_plan("prepare")
    tg.change(p, BUCKET_G)["change"]["after"]["something_else"] = 1
    assert any(BUCKET_G in x and "only" in x for x in run_glue(p, "prepare", gstack))
    p = glue_plan("prepare")
    tg.change(p, gpc.JOB)["change"]["actions"] = ["update"]
    assert any(gpc.JOB in x for x in run_glue(p, "prepare", gstack))


def test_glue_destroy_refuses_a_flag_that_is_not_true_a_missing_delete_or_another_stack(
    gstack: Path,
) -> None:
    p = glue_plan("destroy")
    tg.change(p, BUCKET_G)["change"]["before"]["force_destroy"] = False
    assert any(BUCKET_G in x and "force_destroy" in x for x in run_glue(p, "destroy", gstack))
    p = glue_plan("destroy")
    p["resource_changes"] = [r for r in p["resource_changes"] if r["address"] != gpc.JOB]
    assert any(gpc.JOB in x for x in run_glue(p, "destroy", gstack))
    p = glue_plan("destroy")
    p["resource_changes"].append(
        {"address": "aws_vpc.batch", "mode": "managed", "type": "aws_vpc", "name": "batch",
         "change": {"actions": ["delete"], "before": {}, "after": None}}
    )  # fmt: skip
    assert any("aws_vpc.batch" in x for x in run_glue(p, "destroy", gstack))


@pytest.mark.parametrize("actions", [["no-op"], ["update"], ["create"], ["delete", "create"]])
def test_glue_destroy_accepts_deletes_only(gstack: Path, actions: list[str]) -> None:
    p = glue_plan("destroy")
    tg.change(p, gpc.JOB)["change"]["actions"] = actions
    assert any(gpc.JOB in x for x in run_glue(p, "destroy", gstack))


@pytest.mark.parametrize("mode", ["prepare", "destroy"])
@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(errored=True),
        lambda p: p.update(deferred_changes=[{"address": "x"}]),
        lambda p: p.update(resource_drift=[{"address": "x"}]),
        lambda p: p.update(output_changes={"o": {"actions": ["delete"]}}),
        lambda p: p["variables"].update(extra={"value": "x"}),
        lambda p: p["prior_state"]["values"]["root_module"]["resources"].pop(),
    ],
)
def test_glue_teardown_modes_keep_the_soundness_and_state_checks(
    gstack: Path, mode: str, mutate: Any
) -> None:
    p = glue_plan(mode)
    mutate(p)
    assert run_glue(p, mode, gstack)


def test_glue_teardown_modes_keep_the_source_checks_and_refuse_each_others_plans(
    gstack: Path,
) -> None:
    assert run_glue(glue_plan("destroy"), "prepare", gstack)
    assert run_glue(glue_plan("prepare"), "destroy", gstack)
    assert run_glue(glue_plan("prepare"), "update", gstack)
    (gstack / "extra.tf").write_text('module "m" { source = "./x" }\n')
    for mode in ("prepare", "destroy"):
        assert any("module block" in x for x in run_glue(glue_plan(mode), mode, gstack))


def test_glue_main_accepts_the_new_modes(
    gstack: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(glue_plan("destroy")))
    assert gpc.main(["--stack-dir", str(gstack), "--change", "destroy", str(path)]) == 0
    assert "OK    the reviewed shape" in capsys.readouterr().out
    assert gpc.main(["--stack-dir", str(gstack), "--change", "prepare", str(path)]) == 1


# --- the stacks' source carries the flags, for the closing teardown only ------------------------


@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("infra/batch/storage.tf", "force_destroy = true"),
        ("infra/batch/ecr.tf", "force_delete         = true"),
        ("infra/glue/storage.tf", "force_destroy = true"),
    ],
)
def test_the_stack_source_sets_the_emptying_flags_with_the_reason(path: str, line: str) -> None:
    text = (Path(__file__).parents[1] / path).read_text()
    assert line in text
    assert "closing teardown" in text  # a comment says what the flag is for
