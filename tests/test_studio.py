"""Tests for Switch Studio multi-room workflow, v2 template validity, and Switch governance proofs."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from switch_studio.protocol import SwitchRefusalError
from switch_studio.workflow import (
    finalize_publish_after_human_approval,
    prove_switch_central,
    provision_studio,
    run_cut_room_render_phase,
    run_fact_checker_turn,
    run_showrunner_turn,
)


def test_template_v2_structure() -> None:
    template_path = Path(__file__).resolve().parent.parent / "studio.template.yaml"
    data = yaml.safe_load(template_path.read_text())
    assert data["version"] == 2
    resources = data["resources"]
    assert {"showrunner", "fact_checker", "cut_room_bot", "studio_group", "desk", "lab"} <= set(resources.keys())
    # Cut room bot must be in desk to assume the room-scoped exclusive on-air-director role
    assert "cut_room_bot" in resources["desk"]["agents"]
    desk_roles = {r["name"]: r for r in resources["desk"]["roles"]}
    assert desk_roles["on-air-director"]["exclusive"] is True


def test_end_to_end_studio_workflow(tmp_path: Path) -> None:
    env = provision_studio("pulse")
    script_doc_id = run_showrunner_turn(env)
    ledger_doc_id = run_fact_checker_turn(env, script_doc_id)
    render_info = run_cut_room_render_phase(env, script_doc_id, ledger_doc_id, tmp_path)

    assert render_info["role_lease"]["role"] == "on-air-director"
    assert render_info["upload"]["count"] == 2
    assert (tmp_path / "storyboard.svg").exists()
    assert (tmp_path / "narration.wav").exists()

    # Without human approval, finalize_publish_after_human_approval must refuse
    with pytest.raises(SwitchRefusalError) as exc_info:
        finalize_publish_after_human_approval(env, tmp_path)
    assert exc_info.value.code == "human_approval_required"

    # Human posts approval; Cut Room Bot verifies sender type via list_participants and publishes
    env.harness.post_human_message(
        env.desk_id,
        env.harness.owner_id,
        "@pulse-cut-room-bot Approved. Publish it.",
        targets=["pulse-cut-room-bot"],
    )
    receipt = finalize_publish_after_human_approval(env, tmp_path)
    assert receipt["approved_by_human"] == "@tejas:switch.local"
    assert receipt["media_count"] == 2
    assert receipt["schedules_count"] == 1
    assert len(receipt["sha256_receipt"]) == 64


def test_prove_switch_central_boundaries(tmp_path: Path) -> None:
    env = provision_studio("pulse")
    script_doc_id = run_showrunner_turn(env)
    ledger_doc_id = run_fact_checker_turn(env, script_doc_id)
    run_cut_room_render_phase(env, script_doc_id, ledger_doc_id, tmp_path)

    proofs = prove_switch_central(env, tmp_path)
    assert len(proofs) == 4
    codes = [p["code"] for p in proofs]
    assert codes == [
        "single_writer_violation",
        "exclusive_role_occupied",
        "human_approval_required",
        "cross_room_denied",
    ]
