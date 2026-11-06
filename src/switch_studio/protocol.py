"""Switch Agent Protocol client and faithful in-memory Switch Agent Bridge harness.

Speaks the exact routes and wire payloads of `switch_core.bridges.agent`:
- `GET  /agents/{agent_id}/events?connection_id=...&scope=all&filter=addressed&rooms=...`
- `POST /agents/{agent_id}/connection/beat` (`{"connection_id": ..., "generation": 1}`)
- `POST /agents/{agent_id}/ops/{operation}` (unified MCP / HTTP front-door operations)
- `POST /agents/{agent_id}/rooms/{room_id}/media` (multipart attachment upload)

Enforces Switch's 4 core room governance invariants:
1. Single-writer room-scoped documents (`update_room_document` checks `created_by == caller`).
2. Exclusive assumable role leases (`exclusive: true` roles reject a second holder while leased;
   lapsed 6s connection heartbeats auto-release the lease).
3. Cross-room `read_context(room_id=...)` requires membership or a declared room link.
4. Verified participant identity via `list_participants` (`type: "human" | "agent"`) so an
   agent cannot spoof a human producer's `Approved. Publish it.` sign-off.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib import request as urllib_request

HEARTBEAT_TTL_SECONDS = 6.0


class SwitchRefusalError(RuntimeError):
    """Raised when Switch refuses an operation with 400/403/404/409/422."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"[{status_code} {code}] {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass
class RoomDoc:
    id: str
    room_id: str
    name: str
    description: str
    instructions: str
    content: str
    created_by: str  # transport_user_id or agent_id
    author_type: str  # "human" | "agent"


@dataclass
class RoomRole:
    name: str
    instructions: str
    exclusive: bool = False
    holders: dict[str, str] = field(default_factory=dict)  # agent_id -> connection_id


@dataclass
class RoomScheduleRecord:
    id: str
    room_id: str
    prompt: str
    agent_ids: list[str]
    cron_expr: str | None
    timezone: str
    created_by: str
    enabled: bool = True


@dataclass
class Participant:
    id: str  # transport id e.g. "@tejas:switch.local" or agent UUID
    name: str
    type: str  # "human" | "agent"
    status: str | None = "online"
    alias: str | None = None
    addressing: str = "owner-agents"  # "anyone" | "owner-agents" | "owner"
    anyone_in_rooms: set[str] = field(default_factory=set)


@dataclass
class ConnectionRecord:
    connection_id: str
    agent_id: str
    generation: int = 1
    rooms: set[str] = field(default_factory=set)
    connected_room: str | None = None
    last_beat_at: float = field(default_factory=time.monotonic)
    cursor: int = 0


class SwitchBridgeHarness:
    """Deterministic Switch Agent Bridge server state for local runs and Switch-off proofs."""

    def __init__(self, owner_id: str = "@tejas:switch.local", owner_name: str = "tejas") -> None:
        self.owner_id = owner_id
        self.owner_name = owner_name
        self.online = True
        self.seq = 0
        self.participants: dict[str, Participant] = {
            owner_id: Participant(id=owner_id, name=owner_name, type="human", addressing="anyone")
        }
        self.api_keys: dict[str, str] = {}  # token -> agent_id
        self.rooms: dict[str, dict[str, Any]] = {}
        self.room_members: dict[str, set[str]] = {}
        self.room_links: dict[str, set[str]] = {}
        self.room_docs: dict[str, dict[str, RoomDoc]] = {}
        self.room_roles: dict[str, dict[str, RoomRole]] = {}
        self.room_messages: dict[str, list[dict[str, Any]]] = {}
        self.room_media: dict[str, list[dict[str, Any]]] = {}
        self.room_schedules: dict[str, list[RoomScheduleRecord]] = {}
        self.connections: dict[str, ConnectionRecord] = {}
        self.agent_events: dict[str, list[dict[str, Any]]] = {}

    def register_agent(
        self,
        name: str,
        *,
        addressing: str = "owner-agents",
        anyone_in_rooms: set[str] | None = None,
    ) -> tuple[str, str]:
        agent_id = f"agt_{name.replace('-', '_')}"
        token = f"swk_{uuid.uuid4().hex}"
        self.participants[agent_id] = Participant(
            id=agent_id,
            name=name,
            type="agent",
            addressing=addressing,
            anyone_in_rooms=set(anyone_in_rooms or set()),
        )
        self.api_keys[token] = agent_id
        self.agent_events.setdefault(agent_id, [])
        return agent_id, token

    def create_room(
        self,
        room_id: str,
        name: str,
        *,
        description: str,
        instructions: str,
        members: list[str],
        roles: list[RoomRole] | None = None,
        docs: list[RoomDoc] | None = None,
    ) -> None:
        self.rooms[room_id] = {
            "id": room_id,
            "name": name,
            "description": description,
            "instructions": instructions,
        }
        self.room_members[room_id] = set(members)
        self.room_links.setdefault(room_id, set())
        self.room_docs[room_id] = {d.id: d for d in (docs or [])}
        self.room_roles[room_id] = {r.name: r for r in (roles or [])}
        self.room_messages[room_id] = []
        self.room_media[room_id] = []
        self.room_schedules[room_id] = []

    def link_rooms(self, from_room: str, to_room: str) -> None:
        self.room_links.setdefault(from_room, set()).add(to_room)
        self.room_links.setdefault(to_room, set()).add(from_room)

    def _require_online(self) -> None:
        if not self.online:
            raise SwitchRefusalError(503, "switch_offline", "Switch Agent Bridge is offline; room bus unreachable.")

    def _auth(self, token: str) -> Participant:
        self._require_online()
        agent_id = self.api_keys.get(token)
        if not agent_id or agent_id not in self.participants:
            raise SwitchRefusalError(401, "unauthorized", "Invalid agent bridge token.")
        return self.participants[agent_id]

    def _sweep_expired_leases(self, now: float | None = None) -> None:
        ts = now if now is not None else time.monotonic()
        expired_conns = [
            cid for cid, c in self.connections.items() if ts - c.last_beat_at > HEARTBEAT_TTL_SECONDS
        ]
        for cid in expired_conns:
            conn = self.connections.pop(cid)
            for roles in self.room_roles.values():
                for role in roles.values():
                    if role.holders.get(conn.agent_id) == cid:
                        role.holders.pop(conn.agent_id, None)

    def open_stream(self, token: str, connection_id: str, rooms: list[str]) -> dict[str, Any]:
        agent = self._auth(token)
        for rid in rooms:
            if agent.id not in self.room_members.get(rid, set()):
                raise SwitchRefusalError(403, "not_room_member", f"{agent.name} is not a member of {rid}")
        prev = self.connections.get(connection_id)
        gen = (prev.generation + 1) if prev else 1
        conn = ConnectionRecord(
            connection_id=connection_id,
            agent_id=agent.id,
            generation=gen,
            rooms=set(rooms),
            connected_room=rooms[0] if len(rooms) == 1 else None,
        )
        self.connections[connection_id] = conn
        return {
            "event": "connection_state",
            "data": {"connection_id": connection_id, "generation": gen, "rooms": sorted(rooms)},
        }

    def beat(self, token: str, connection_id: str, generation: int, now: float | None = None) -> dict[str, Any]:
        agent = self._auth(token)
        self._sweep_expired_leases(now)
        conn = self.connections.get(connection_id)
        if conn is None or conn.agent_id != agent.id:
            raise SwitchRefusalError(404, "unknown_connection", f"Connection {connection_id} is not active")
        if conn.generation != generation:
            raise SwitchRefusalError(409, "taken_over", "Connection generation superseded")
        conn.last_beat_at = now if now is not None else time.monotonic()
        return {"ok": True, "rooms": sorted(conn.rooms), "cursor": conn.cursor, "position": {}}

    def _require_conn(self, agent_id: str, connection_id: str) -> ConnectionRecord:
        self._sweep_expired_leases()
        conn = self.connections.get(connection_id)
        if conn is None or conn.agent_id != agent_id:
            raise SwitchRefusalError(409, "no_stream", "Open an SSE/WS stream before calling room operations")
        return conn

    def call_op(self, token: str, connection_id: str, operation: str, args: dict[str, Any]) -> Any:
        agent = self._auth(token)
        conn = self._require_conn(agent.id, connection_id)

        if operation == "connect_to_room":
            room_id = args["room_id"]
            if agent.id not in self.room_members.get(room_id, set()):
                raise SwitchRefusalError(403, "forbidden", f"{agent.name} is not a member of {room_id}")
            conn.connected_room = room_id
            conn.rooms.add(room_id)
            room = self.rooms[room_id]
            return {
                "room_id": room_id,
                "name": room["name"],
                "instructions": f"--- BEGIN ROOM INSTRUCTIONS ---\n{room['instructions']}\n--- END ROOM INSTRUCTIONS ---",
                "documents": [
                    {"id": d.id, "name": d.name, "description": d.description, "created_by": d.created_by}
                    for d in self.room_docs[room_id].values()
                ],
                "roles": [
                    {"name": r.name, "exclusive": r.exclusive, "holders": list(r.holders.keys())}
                    for r in self.room_roles[room_id].values()
                ],
                "linked_rooms": sorted(self.room_links.get(room_id, set())),
            }

        room_id = conn.connected_room
        if not room_id:
            raise SwitchRefusalError(400, "not_connected", "Call connect_to_room first")

        if operation == "load_internal_documents":
            ids = args["ids"]
            out = []
            for doc_id in ids:
                doc = self.room_docs[room_id].get(doc_id)
                if not doc:
                    raise SwitchRefusalError(404, "doc_not_found", f"Document {doc_id} is not attached to {room_id}")
                out.append({"id": doc.id, "name": doc.name, "description": doc.description, "content": doc.content})
            return out

        if operation == "create_room_document":
            name = args["name"]
            for existing in self.room_docs[room_id].values():
                if existing.name == name:
                    raise SwitchRefusalError(400, "duplicate_doc", f"Document {name!r} already exists in {room_id}")
            doc_id = f"doc_{uuid.uuid4().hex[:10]}"
            self.room_docs[room_id][doc_id] = RoomDoc(
                id=doc_id,
                room_id=room_id,
                name=name,
                description=args["description"],
                instructions=args["instructions"],
                content=args["content"],
                created_by=agent.id,
                author_type="agent",
            )
            return {"document_id": doc_id}

        if operation == "update_room_document":
            doc_id = args["document_id"]
            doc = self.room_docs[room_id].get(doc_id)
            if not doc:
                raise SwitchRefusalError(404, "doc_not_found", f"Document {doc_id} not found in {room_id}")
            # Single-writer rule: only the creating agent can update its own document;
            # human-authored documents (like creative-brief) cannot be overwritten by any agent.
            if doc.created_by != agent.id:
                raise SwitchRefusalError(
                    403,
                    "single_writer_violation",
                    f"Only the creator ({doc.created_by}) may update document {doc.name!r}; caller is {agent.id}.",
                )
            if args.get("content") is not None:
                doc.content = args["content"]
            if args.get("description") is not None:
                doc.description = args["description"]
            return {"document_id": doc_id}

        if operation == "assume_role":
            role_name = args["role"]
            role = self.room_roles[room_id].get(role_name)
            if not role:
                raise SwitchRefusalError(404, "role_not_found", f"Role {role_name!r} does not exist in {room_id}")
            if role.exclusive and role.holders and agent.id not in role.holders:
                current_holder = next(iter(role.holders.keys()))
                raise SwitchRefusalError(
                    409,
                    "exclusive_role_occupied",
                    f"Exclusive role {role_name!r} in {room_id} is already leased by {current_holder}.",
                )
            role.holders[agent.id] = conn.connection_id
            return {"role": role.name, "exclusive": role.exclusive, "instructions": role.instructions}

        if operation == "release_role":
            released = []
            for role in self.room_roles[room_id].values():
                if agent.id in role.holders:
                    role.holders.pop(agent.id, None)
                    released.append(role.name)
            return {"released": released}

        if operation == "read_context":
            target_room = args.get("room_id") or room_id
            if target_room != room_id:
                linked = target_room in self.room_links.get(room_id, set())
                member = agent.id in self.room_members.get(target_room, set())
                if not (linked and member):
                    raise SwitchRefusalError(
                        403,
                        "cross_room_denied",
                        f"Cannot cross-read {target_room} from {room_id}: rooms are not linked or caller is not a member.",
                    )
            limit = int(args.get("limit", 50))
            msgs = self.room_messages.get(target_room, [])[-limit:]
            docs = [
                {"id": d.id, "name": d.name, "description": d.description, "created_by": d.created_by}
                for d in self.room_docs.get(target_room, {}).values()
            ]
            return {"room_id": target_room, "messages": list(msgs), "documents": docs}

        if operation == "list_participants":
            members = self.room_members.get(room_id, set())
            return [
                {
                    "id": self.participants[mid].id,
                    "name": self.participants[mid].name,
                    "type": self.participants[mid].type,
                    "status": self.participants[mid].status,
                    "alias": self.participants[mid].alias,
                }
                for mid in sorted(members)
                if mid in self.participants
            ]

        if operation == "post_message":
            return self._record_message(room_id, sender=agent, body=args["body"], targets=[], thread_id=args.get("thread_id"))

        if operation == "send_targeted_message":
            targets = list(args.get("target_names") or []) + list(args.get("target_roles") or [])
            prefix = " ".join(f"@{t}" for t in targets)
            body = f"{prefix} {args['body']}".strip()
            return self._record_message(room_id, sender=agent, body=body, targets=targets, thread_id=args.get("thread_id"))

        if operation == "create_schedule":
            sched = RoomScheduleRecord(
                id=f"sch_{uuid.uuid4().hex[:10]}",
                room_id=room_id,
                prompt=args["prompt"],
                agent_ids=list(args["agent_ids"]),
                cron_expr=args.get("cron_expr"),
                timezone=args.get("timezone", "UTC"),
                created_by=agent.id,
            )
            self.room_schedules[room_id].append(sched)
            return {
                "id": sched.id,
                "room_id": sched.room_id,
                "prompt": sched.prompt,
                "agent_ids": sched.agent_ids,
                "cron_expr": sched.cron_expr,
                "timezone": sched.timezone,
            }

        if operation == "list_schedules":
            return [
                {
                    "id": s.id,
                    "room_id": s.room_id,
                    "prompt": s.prompt,
                    "agent_ids": s.agent_ids,
                    "cron_expr": s.cron_expr,
                    "timezone": s.timezone,
                }
                for s in self.room_schedules.get(room_id, [])
            ]

        raise SwitchRefusalError(404, "unknown_operation", f"Unknown operation {operation!r}")

    def upload_media(
        self,
        token: str,
        room_id: str,
        files: list[tuple[bytes, str, str]],
        caption: str | None = None,
    ) -> dict[str, Any]:
        agent = self._auth(token)
        if agent.id not in self.room_members.get(room_id, set()):
            raise SwitchRefusalError(403, "forbidden", f"{agent.name} is not in {room_id}")
        if not files or any(len(raw) == 0 for raw, _, _ in files):
            raise SwitchRefusalError(400, "empty_media", "Attachment payload cannot be empty")
        event_ids = []
        for raw, filename, content_type in files:
            ev_id = f"$media_{uuid.uuid4().hex[:10]}"
            entry = {
                "event_id": ev_id,
                "room_id": room_id,
                "sender": agent.id,
                "filename": filename,
                "content_type": content_type,
                "size_bytes": len(raw),
                "caption": caption,
            }
            self.room_media[room_id].append(entry)
            event_ids.append(ev_id)
        if caption:
            self._record_message(room_id, sender=agent, body=f"[attachment: {', '.join(f for _, f, _ in files)}] {caption}", targets=[])
        return {"ok": True, "event_ids": event_ids, "count": len(event_ids)}

    def post_human_message(self, room_id: str, human_id: str, body: str, targets: list[str] | None = None) -> dict[str, str]:
        self._require_online()
        human = self.participants[human_id]
        return self._record_message(room_id, sender=human, body=body, targets=list(targets or []))

    def _record_message(
        self,
        room_id: str,
        *,
        sender: Participant,
        body: str,
        targets: list[str],
        thread_id: str | None = None,
    ) -> dict[str, str]:
        self.seq += 1
        event_id = f"$msg_{self.seq:04d}"
        # Notice: SSE / read_context messages carry opaque `sender` id, NOT `sender_type`,
        # matching real Switch where `list_participants` must be consulted to verify human type.
        msg = {
            "id": event_id,
            "seq": self.seq,
            "room_id": room_id,
            "sender": sender.id,
            "body": body,
            "thread_id": thread_id,
        }
        self.room_messages[room_id].append(msg)
        for member_id in self.room_members.get(room_id, set()):
            if member_id == sender.id:
                continue
            recipient = self.participants.get(member_id)
            if not recipient or recipient.type != "agent":
                continue
            if self._allows_delivery(recipient, sender, room_id, targets):
                self.agent_events.setdefault(recipient.id, []).append(msg)
        return {"event_id": event_id}

    def _allows_delivery(self, recipient: Participant, sender: Participant, room_id: str, targets: list[str]) -> bool:
        if targets and recipient.name not in targets:
            # Check if addressed by role
            held_roles = [
                r.name for r in self.room_roles.get(room_id, {}).values() if recipient.id in r.holders
            ]
            if not any(hr in targets for hr in held_roles):
                return False
        if recipient.addressing == "anyone":
            return True
        if recipient.addressing == "owner":
            return sender.id == self.owner_id
        if recipient.addressing == "owner-agents":
            if room_id in recipient.anyone_in_rooms:
                return True
            return sender.id == self.owner_id or sender.type == "agent"
        return False


class AgentProtocolClient:
    """Client spoken by each Switch Studio agent over HTTP or the local SwitchBridgeHarness."""

    def __init__(
        self,
        *,
        agent_id: str,
        token: str,
        harness: SwitchBridgeHarness | None = None,
        base_url: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.token = token
        self.harness = harness
        self.base_url = base_url.rstrip("/") if base_url else None
        self.connection_id = str(uuid.uuid4())
        self.generation = 1

    def open_stream(self, rooms: list[str]) -> dict[str, Any]:
        if self.harness is not None:
            frame = self.harness.open_stream(self.token, self.connection_id, rooms)
            self.generation = int(frame["data"]["generation"])
            return frame
        raise RuntimeError("Configure SwitchBridgeHarness or live SWITCH_URL")

    def beat(self, now: float | None = None) -> dict[str, Any]:
        if self.harness is not None:
            return self.harness.beat(self.token, self.connection_id, self.generation, now=now)
        return self._http_post(
            f"/agents/{self.agent_id}/connection/beat",
            {"connection_id": self.connection_id, "generation": self.generation},
        )

    def op(self, operation: str, **kwargs: Any) -> Any:
        if self.harness is not None:
            return self.harness.call_op(self.token, self.connection_id, operation, kwargs)
        return self._http_post(f"/agents/{self.agent_id}/ops/{operation}", kwargs)

    def upload_media(self, room_id: str, files: list[tuple[bytes, str, str]], caption: str) -> dict[str, Any]:
        if self.harness is not None:
            return self.harness.upload_media(self.token, room_id, files, caption=caption)
        raise RuntimeError("Multipart media upload requires harness or live Switch server")

    def _http_post(self, path: str, payload: dict[str, Any]) -> Any:
        req = urllib_request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-Switch-Connection-Id": self.connection_id,
            },
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
