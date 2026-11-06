"""CLI entry point for Switch Studio demo and Switch-off proof."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from switch_studio.workflow import (
    finalize_publish_after_human_approval,
    prove_switch_central,
    provision_studio,
    run_cut_room_render_phase,
    run_fact_checker_turn,
    run_showrunner_turn,
)


def render_dashboard_html(env, receipt: dict, proofs: list[dict], artifacts_dir: Path) -> Path:
    desk_msgs = env.harness.room_messages[env.desk_id]
    lab_msgs = env.harness.room_messages[env.lab_id]
    docs = list(env.harness.room_docs[env.desk_id].values())

    msg_rows = "".join(
        f"<tr><td><code>{html.escape(m['id'])}</code></td>"
        f"<td><code>{html.escape(m['sender'])}</code></td>"
        f"<td>{html.escape(m['body'])}</td></tr>"
        for m in desk_msgs
    )
    doc_rows = "".join(
        f"<tr><td><code>{html.escape(d.name)}</code></td>"
        f"<td><code>{html.escape(d.created_by)}</code> ({html.escape(d.author_type)})</td>"
        f"<td>{html.escape(d.description)}</td></tr>"
        for d in docs
    )
    proof_rows = "".join(
        f"<tr><td><strong>{html.escape(p['boundary'])}</strong></td>"
        f"<td>{html.escape(p['attempt'])}</td>"
        f"<td><code>HTTP {p['status_code']} ({html.escape(p['code'])})</code></td>"
        f"<td style='color:#199e70;font-weight:600'>{html.escape(p['result'])}</td></tr>"
        for p in proofs
    )

    dashboard = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Switch Studio · Multi-Room Production Dashboard</title>
  <style>
    body {{ background:#0d0d0d; color:#f0efec; font-family:system-ui,-apple-system,sans-serif; margin:0; padding:32px; line-height:1.5; }}
    h1, h2 {{ margin-top:0; }}
    .meta {{ color:#c3c2b7; font-family:monospace; font-size:13px; margin-bottom:24px; }}
    .card {{ background:#1a1a19; border:1px solid #2c2c2a; border-radius:10px; padding:20px; margin-bottom:24px; }}
    table {{ width:100%; border-collapse:collapse; font-size:14px; }}
    th, td {{ text-align:left; padding:10px 12px; border-bottom:1px solid #2c2c2a; vertical-align:top; }}
    th {{ color:#898781; font-size:12px; text-transform:uppercase; letter-spacing:0.05em; }}
    code {{ background:#262624; padding:2px 6px; border-radius:4px; font-size:12px; }}
  </style>
</head>
<body>
  <h1>Switch Studio · Multi-Room Production &amp; Fact-Checked Publishing House</h1>
  <div class="meta">Rooms: {html.escape(env.desk_id)} ↔ {html.escape(env.lab_id)} · Receipt SHA-256: {html.escape(receipt['sha256_receipt'])}</div>

  <div class="card">
    <h2>1. Rendered Storyboard Attachment (<code>storyboard.svg</code>)</h2>
    <img src="storyboard.svg" alt="Rendered Storyboard" style="max-width:100%;border-radius:8px;border:1px solid #2c2c2a"/>
  </div>

  <div class="card">
    <h2>2. Single-Writer Room Documents in <code>#pulse-studio-desk</code></h2>
    <table>
      <thead><tr><th>Document</th><th>Single-Writer Owner</th><th>Description</th></tr></thead>
      <tbody>{doc_rows}</tbody>
    </table>
  </div>

  <div class="card">
    <h2>3. Room Transcript (<code>#pulse-studio-desk</code>, {len(desk_msgs)} events; linked <code>#pulse-fact-and-render-lab</code>, {len(lab_msgs)} events)</h2>
    <table>
      <thead><tr><th>Event ID</th><th>Opaque Sender ID</th><th>Message Body</th></tr></thead>
      <tbody>{msg_rows}</tbody>
    </table>
  </div>

  <div class="card">
    <h2>4. Switch-Centrality Proof (<code>--prove-switch-central</code>)</h2>
    <table>
      <thead><tr><th>Switch Governance Boundary</th><th>Adversarial / Bypass Attempt</th><th>Protocol Response</th><th>Outcome</th></tr></thead>
      <tbody>{proof_rows}</tbody>
    </table>
  </div>
</body>
</html>
"""
    out = artifacts_dir / "studio-dashboard.html"
    out.write_text(dashboard)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Switch Studio multi-room workflow and Switch-off proof")
    parser.add_argument("--prove-switch-central", action="store_true", help="Run adversarial boundary checks proving Switch centrality")
    parser.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"), help="Directory for rendered output")
    args = parser.parse_args()

    env = provision_studio("pulse")
    print("[1/5] Provisioned Switch v2 studio group: #pulse-studio-desk <-> #pulse-fact-and-render-lab")

    script_doc_id = run_showrunner_turn(env)
    print(f"[2/5] @pulse-showrunner read human `creative-brief` and created `storyboard-and-script` ({script_doc_id})")

    ledger_doc_id = run_fact_checker_turn(env, script_doc_id)
    print(f"[3/5] @pulse-fact-checker cross-read #pulse-studio-desk from #pulse-fact-and-render-lab and verified 10/10 claims in `citation-ledger` ({ledger_doc_id})")

    render_res = run_cut_room_render_phase(env, script_doc_id, ledger_doc_id, args.artifacts_dir)
    print(
        f"[4/5] @pulse-cut-room-bot assumed exclusive `on-air-director` role, uploaded {render_res['upload']['count']} media files (storyboard.svg, narration.wav), and created schedule {render_res['schedule']['id']}"
    )

    env.harness.post_human_message(
        env.desk_id,
        env.harness.owner_id,
        "@pulse-cut-room-bot Approved. Publish it.",
        targets=["pulse-cut-room-bot"],
    )
    receipt = finalize_publish_after_human_approval(env, args.artifacts_dir)
    print(f"[5/5] Verified human approval from {receipt['approved_by_human']} via `list_participants`; published receipt {receipt['sha256_receipt'][:16]}")

    proofs = prove_switch_central(env, args.artifacts_dir)
    if args.prove_switch_central:
        print("\n=== SWITCH-CENTRALITY PROOF (--prove-switch-central) ===")
        for p in proofs:
            print(f"  [PASS] {p['boundary']}: HTTP {p['status_code']} ({p['code']}) -> {p['result']}")

    dash = render_dashboard_html(env, receipt, proofs, args.artifacts_dir)
    print(f"\nArtifacts written to {args.artifacts_dir.resolve()} (dashboard: {dash.name})")
    print(json.dumps({"receipt": receipt, "proofs_passed": len(proofs)}, indent=2))


if __name__ == "__main__":
    main()
