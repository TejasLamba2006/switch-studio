# Switch Studio

Multi-room creative production and fact-checked publishing studio built on [Switch](https://github.com/sandbox-quantum/switch) (`version: 2` room templates + Switch Agent Protocol).

Turning technical notes and raw benchmarks into a finished video storyboard, audio narration track, and published article usually breaks in one of two ways: either you paste context between five solo chat tabs until an agent invents a number, or a second agent overwrites your original brief while drafting.

Switch Studio fixes both by running three specialized teammates across two linked Switch rooms (`#pulse-studio-desk` and `#pulse-fact-and-render-lab`), backed by Switch's single-writer room documents, room-scoped exclusive role leases, cross-room context reads, and verified human participant sign-off.

---

## Architecture

```
                      studio.template.yaml (version: 2)
                                     │
         ┌───────────────────────────┴───────────────────────────┐
         ▼                                                       ▼
┌─────────────────────────────────────────┐     ┌─────────────────────────────────────────┐
│ Room 1: #pulse-studio-desk              │     │ Room 2: #pulse-fact-and-render-lab      │
│ (channel_public · linked to lab)        │◄───►│ (channel_private · verification lab)    │
│                                         │     │                                         │
│ Docs (Single-Writer Enforced):          │     │ Operations:                             │
│ • creative-brief       (@tejas:human)   │     │ • assume_role("fact-verifier")          │
│ • storyboard-and-script (@showrunner)   │     │ • read_context(room_id=desk)            │
│ • citation-ledger      (@fact-checker)  │     │ • verifies 10/10 telemetry claims       │
│                                         │     │                                         │
│ Exclusive Role Lease:                   │     │ Agents:                                 │
│ • on-air-director (6s heartbeat TTL)    │     │ • @pulse-fact-checker                   │
│                                         │     │ • @pulse-cut-room-bot                   │
│ Media & Schedules:                      │     └─────────────────────────────────────────┘
│ • POST /agents/{id}/rooms/{room}/media  │
│   -> storyboard.svg + narration.wav     │
│ • create_schedule("0 9 * * *")          │
│ • list_participants() human type check  │
└─────────────────────────────────────────┘
```

---

## Why Switch Is Load-Bearing

Every stage of the pipeline relies on a Switch primitive that fails closed if bypassed (`bash demo.sh --prove-switch-central`):

| Switch Primitive | How Switch Studio Uses It | What Happens Without Switch (`--prove-switch-central`) |
|---|---|---|
| **Single-Writer Room Docs** (`create_room_document`, `update_room_document`, `load_internal_documents`) | The human producer owns `creative-brief`; `@pulse-showrunner` owns `storyboard-and-script`; `@pulse-fact-checker` owns `citation-ledger`. | When `@pulse-showrunner` tries to call `update_room_document` on `creative-brief`, Switch rejects the write with `HTTP 403 (single_writer_violation)`. |
| **Exclusive Assumable Role Leases** (`assume_role`, `release_role`, `/connection/beat`) | `@pulse-cut-room-bot` is a member of `#pulse-studio-desk` and assumes the exclusive `on-air-director` role before rendering and uploading room media. | A second agent attempting `assume_role("on-air-director")` while leased is rejected with `HTTP 409 (exclusive_role_occupied)`. If the holder stops beating for 6s, Switch auto-releases the lease. |
| **Participant Identity Verification** (`list_participants`) | SSE and `read_context` events carry opaque transport sender IDs (`@tejas:switch.local`, `agt_...`). Before publishing, `@pulse-cut-room-bot` resolves the sender of `Approved. Publish it.` against `list_participants` and checks `type == "human"`. | When an agent tries to self-approve by posting `Approved. Publish it.`, `@pulse-cut-room-bot` sees `type == "agent"` in `list_participants` and refuses to publish (`HTTP 403 human_approval_required`). |
| **Linked Multi-Room Graph** (`group`, `links`, `read_context(room_id=...)`) | `@pulse-fact-checker` sits in `#pulse-fact-and-render-lab` and cross-reads `#pulse-studio-desk` via `read_context(room_id=desk_id)` without hopping rooms. | Severing the room link or taking the Switch Agent Bridge offline immediately blocks cross-room reads with `HTTP 403 (cross_room_denied)`. |
| **Room Media & Schedules** (`POST /agents/{id}/rooms/{room_id}/media`, `create_schedule`) | Uploads the rendered 3-scene `storyboard.svg` and PCM `narration.wav` straight into the room and schedules a recurring 09:00 IST retention check (`#771`). | No shared room artifact delivery or cron follow-up to the room's agents. |

---

## Quickstart (One Command)

```bash
git clone https://github.com/TejasLamba2006/switch-studio.git
cd switch-studio
bash demo.sh
```

Running `bash demo.sh`:
1. Validates `studio.template.yaml` against `switch_core.template_lint` and `switch_core.template_v2.compile_v2` (0 errors, 0 warnings).
2. Provisions `#pulse-studio-desk` and `#pulse-fact-and-render-lab` with `@pulse-showrunner`, `@pulse-fact-checker`, and `@pulse-cut-room-bot`.
3. Runs the 5-stage production workflow and generates:
   - `artifacts/storyboard.svg` — 1200x630 3-scene visual storyboard with verified telemetry badges
   - `artifacts/narration.wav` — synthesized 16 kHz PCM audio cue track
   - `artifacts/publish-receipt.json` — SHA-256 signed release receipt tied to the human approval event ID
   - `artifacts/studio-dashboard.html` — self-contained visual inspection dashboard
4. Executes the 4 adversarial boundary checks (`--prove-switch-central`).

---

## Running the Test Suite

```bash
pytest -v
```

---

## Using `studio.template.yaml` in Switch Console

Open Switch Console -> **Create from template** -> load `studio.template.yaml`, or call the MCP tool from any connected agent:

```python
run_template(
    template_id="studio",
    inputs={
        "studio_name": "pulse",
        "episode_topic": "Why 211k Turn-1 Context Bloat Happens and How Binary Patching Cuts It 89%",
    },
)
```
