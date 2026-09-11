"""
main.py
AI Cluster Doctor - backend API.

Run with:
    uvicorn main:app --reload --port 8000

Endpoints:
    GET   /api/nodes                    -> current status for every (visible) node
                                            optional ?search= filters by id/hostname/nickname
    GET   /api/nodes/{node_id}          -> current status for one node
    GET   /api/nodes/{node_id}/history  -> raw telemetry history for charts
    GET   /api/nodes/{node_id}/actions  -> action history for one node
    PATCH /api/nodes/{node_id}/nickname -> rename a device (body: {"nickname": "..."})
    DELETE /api/nodes/{node_id}         -> remove a device from the visible inventory
    GET   /api/alerts                   -> nodes currently in warning/critical state
    POST  /api/chat                     -> ask the cluster-doctor assistant a question
    POST  /api/agent/telemetry          -> Agents push a telemetry reading here (X-API-Key)
    POST  /api/auth/login               -> {"username","password"} -> JWT access token

All endpoints above except /api/auth/login and /api/agent/telemetry require
a valid JWT (Authorization: Bearer <token>); nickname/delete additionally
require the "admin" role. See auth.py.
"""

import asyncio
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import auth
import db
from providers import TelemetryProvider, SimulatorProvider, CompositeProvider
from agent_provider import AgentProvider
from discovery import start_discovery_responder
from llm_provider import get_llm_provider
from ml import compute_health
from score_audit import audit_score
from action_planner import plan_action
from action_logger import init_action_log, save_action, get_actions, demo_resolve_latest

import logging
import os
import secrets

logger = logging.getLogger("cluster_doctor_host")

TICK_SECONDS = 2.0

# `sim` is typed as the abstract interface; CompositeProvider lets it cover
# both the simulator and real Agents at once. Adding DCGM/Prometheus later
# is just another entry in this list - main.py and ml.py don't change.
agent_provider = AgentProvider()
sim: TelemetryProvider = CompositeProvider([SimulatorProvider(), agent_provider])

# None if CLUSTER_DOCTOR_LLM_PROVIDER isn't set, or if the configured
# provider failed to initialize (e.g. missing API key) - either way,
# answer_query() below falls back to the offline rule-based assistant.
llm_provider = get_llm_provider()


async def telemetry_loop():
    while True:
        sim.tick()
        await asyncio.sleep(TICK_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    init_action_log()

    if db.user_count() == 0:
        admin_username = os.environ.get("ADMIN_USERNAME", "admin")
        admin_password = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(12)
        db.create_user(admin_username, auth.hash_password(admin_password), role="admin")
        logger.warning(
            "No users existed yet - created initial admin account. "
            "Username: %s  Password: %s  (save this now; change it via a future "
            "user-management endpoint, this password is only ever shown once)",
            admin_username, admin_password,
        )

    logger.warning("Agent API key (put this in each Agent's CLUSTER_DOCTOR_API_KEY): %s", auth.get_agent_api_key())

    # warm up some history so the ML model has data to work with immediately
    for _ in range(45):
        sim.tick()
    task = asyncio.create_task(telemetry_loop())
    discovery_transport = await start_discovery_responder()
    yield
    task.cancel()
    discovery_transport.close()


app = FastAPI(title="AI Cluster Doctor API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _source_for(node_id: str) -> str:
    return "agent" if agent_provider.node_exists(node_id) else "simulator"


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(req: LoginRequest):
    user = db.get_user(req.username)
    if not user or not auth.verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = auth.create_access_token(user["username"], user["role"])
    return {"access_token": token, "token_type": "bearer", "role": user["role"]}


def _node_summary(node_id: str) -> dict:
    history = sim.get_history(node_id)
    health = compute_health(history)
    latest = history[-1] if history else {}
    info = sim.get_node_info(node_id)

    # register this sighting in the persistent device registry (survives restarts)
    db.upsert_seen(node_id, hostname=info["name"], source=_source_for(node_id))
    device = db.get_device(node_id) or {}

    # score explainability + rule-based next-step action (additive - does not
    # change compute_health's own scoring/root_cause logic)
    audit = audit_score(latest, health["anomaly"]["anomaly_score"]) if history else None
    action = plan_action(health["root_cause"], latest, health["health_score"]) if history else None
    if action:
        save_action(
            node_id, action["action"], action["priority"], action["reason"],
            before_score=health["health_score"],
        )
        # prototype-only: auto-resolves with demo numbers so History has
        # something to show without wiring real before/after polling yet
        demo_resolve_latest(node_id)

    return {
        "node_id": node_id,
        "name": info["name"],
        "hostname": info["name"],
        "nickname": device.get("nickname") or info["name"],
        "gpu_model": info["gpu_model"],
        "first_seen": device.get("first_seen"),
        "last_seen": device.get("last_seen"),
        "latest": latest,
        "score_audit": audit,
        "planned_action": action,
        "action_history": get_actions(node_id),
        **health,
    }


def _visible_node_ids() -> list[str]:
    """All node ids the provider knows about, minus any soft-deleted devices."""
    return [nid for nid in sim.get_node_ids() if not db.is_deleted(nid)]


def _ensure_registered(node_id: str) -> None:
    """Make sure this node has a row in the device registry before any
    mutation (rename/delete) is attempted on it - a node may not have been
    registered yet if no GET endpoint has been hit for it since startup."""
    info = sim.get_node_info(node_id)
    db.upsert_seen(node_id, hostname=info["name"], source=_source_for(node_id))


@app.get("/api/nodes")
def get_nodes(search: str | None = None, user: dict = Depends(auth.get_current_user)):
    summaries = [_node_summary(nid) for nid in _visible_node_ids()]
    if search:
        q = search.lower().strip()
        summaries = [
            s for s in summaries
            if q in s["node_id"].lower() or q in s["hostname"].lower() or q in s["nickname"].lower()
        ]
    return summaries


@app.get("/api/nodes/{node_id}")
def get_node(node_id: str, user: dict = Depends(auth.get_current_user)):
    if node_id not in _visible_node_ids():
        return {"error": "node not found"}
    return _node_summary(node_id)


@app.get("/api/nodes/{node_id}/history")
def get_node_history(node_id: str, limit: int = 120, user: dict = Depends(auth.get_current_user)):
    if node_id not in _visible_node_ids():
        return {"error": "node not found"}
    return {"node_id": node_id, "history": sim.get_history(node_id, limit)}


@app.get("/api/nodes/{node_id}/actions")
def get_node_actions(node_id: str, user: dict = Depends(auth.get_current_user)):
    if node_id not in _visible_node_ids():
        return {"error": "node not found"}
    return get_actions(node_id)


class NicknameRequest(BaseModel):
    nickname: str


@app.patch("/api/nodes/{node_id}/nickname")
def rename_node(node_id: str, req: NicknameRequest, user: dict = Depends(auth.require_role("admin"))):
    if node_id not in _visible_node_ids():
        return {"error": "node not found"}
    _ensure_registered(node_id)
    nickname = req.nickname.strip() or None
    db.set_nickname(node_id, nickname)
    return _node_summary(node_id)


@app.delete("/api/nodes/{node_id}")
def delete_node(node_id: str, user: dict = Depends(auth.require_role("admin"))):
    if node_id not in _visible_node_ids():
        return {"error": "node not found"}
    _ensure_registered(node_id)
    db.soft_delete(node_id)
    return {"node_id": node_id, "deleted": True}


@app.get("/api/alerts")
def get_alerts(user: dict = Depends(auth.get_current_user)):
    alerts = []
    for nid in _visible_node_ids():
        summary = _node_summary(nid)
        if summary["status"] != "healthy":
            alerts.append(summary)
    alerts.sort(key=lambda a: a["health_score"])
    return alerts


class ChatRequest(BaseModel):
    message: str


class TelemetryReading(BaseModel):
    temperature_c: float
    gpu_utilization_pct: float
    memory_utilization_pct: float
    power_watts: float
    fan_speed_pct: float


class AgentTelemetryRequest(BaseModel):
    device_id: str
    hostname: str
    reading: TelemetryReading


@app.post("/api/agent/telemetry")
def ingest_agent_telemetry(req: AgentTelemetryRequest, _: None = Depends(auth.verify_agent_key)):
    """Agents call this to report in. Payload uses the same frozen 5-key
    contract SimulatorProvider already produces, so ml.py needs no changes
    to score agent-reported nodes exactly like simulated ones."""
    agent_provider.ingest(req.device_id, req.hostname, req.reading.model_dump())
    return {"status": "ok"}


def _cluster_snapshot():
    return [_node_summary(nid) for nid in _visible_node_ids()]


LLM_SYSTEM_PROMPT = (
    "You are the AI Cluster Doctor assistant, monitoring a GPU cluster (and any "
    "agent-reported PCs). Answer the operator's question using ONLY the telemetry "
    "snapshot provided below - never invent node ids, scores, or metrics that "
    "aren't in it. Be concise (2-4 sentences) and specific, citing node ids and "
    "numbers where relevant. If the snapshot doesn't contain the answer, say so "
    "plainly rather than guessing."
)


def _build_llm_context() -> str:
    nodes = _cluster_snapshot()
    if not nodes:
        return "No telemetry available yet."
    lines = []
    for n in nodes:
        lines.append(
            f"- {n['node_id']} ({n['nickname']}, {n['gpu_model']}): status={n['status']}, "
            f"health_score={n['health_score']}, root_cause={n['root_cause'] or 'none'}, "
            f"latest={n['latest']}, short_term_predictions={n['predictions']}, "
            f"long_term_predictions={n['long_term_predictions']}"
        )
    return "Current cluster telemetry snapshot:\n" + "\n".join(lines)


def _rule_based_reply(message: str) -> str:
    """
    Lightweight rule-based assistant over the live telemetry snapshot.

    This is intentionally dependency-free (no external LLM call) so the demo
    runs fully offline. It's used directly when no LLM provider is
    configured, and as the automatic fallback if the configured provider
    (OpenAI/Anthropic/Ollama - see llm_provider.py) fails for any reason.
    """
    msg = message.lower().strip()
    nodes = _cluster_snapshot()

    if not nodes:
        return "No telemetry available yet."

    critical = [n for n in nodes if n["status"] == "critical"]
    warning = [n for n in nodes if n["status"] == "warning"]
    healthy = [n for n in nodes if n["status"] == "healthy"]

    if re.search(r"\b(status|overview|summary|how.*cluster|health of.*cluster)\b", msg):
        return (
            f"Cluster overview: {len(healthy)} healthy, {len(warning)} in warning, "
            f"{len(critical)} critical, out of {len(nodes)} nodes. "
            + (f"Nodes needing attention: {', '.join(n['node_id'] for n in critical + warning)}."
               if (critical or warning) else "All nodes are within normal operating range.")
        )

    if re.search(r"\b(critical|failing|worst|down)\b", msg):
        if not critical:
            return "No nodes are currently in a critical state."
        lines = [f"{n['node_id']}: score {n['health_score']} - {n['root_cause']}" for n in critical]
        return "Critical nodes:\n" + "\n".join(lines)

    if re.search(r"\b(warn|degrad|risk|attention)", msg):
        if not warning:
            return "No nodes are currently in a warning state."
        lines = [f"{n['node_id']}: score {n['health_score']} - {n['root_cause']}" for n in warning]
        return "Nodes in warning state:\n" + "\n".join(lines)

    node_match = re.search(r"(gpu-node-\d+)", msg)
    if node_match:
        nid = node_match.group(1)
        match = next((n for n in nodes if n["node_id"] == nid), None)
        if not match:
            return f"I don't have telemetry for {nid}."
        preds = match.get("predictions") or {}
        pred_txt = ""
        if preds:
            parts = []
            for feat, p in preds.items():
                if p.get("eta_ticks"):
                    parts.append(f"{feat} trending toward its threshold in ~{p['eta_ticks']} ticks")
            if parts:
                pred_txt = " Prediction: " + "; ".join(parts) + "."
        return (
            f"{nid} ({match['gpu_model']}): health score {match['health_score']} "
            f"({match['status']}). Latest reading: {match['latest']}. "
            f"Root cause: {match['root_cause'] or 'none'}.{pred_txt}"
        )

    if re.search(r"\b(temperature|hot|overheat|thermal)", msg):
        hottest = max(nodes, key=lambda n: n["latest"].get("temperature_c", 0))
        return f"Hottest node right now is {hottest['node_id']} at {hottest['latest']['temperature_c']}°C."

    if re.search(r"\b(predict|trend|about to fail|going to fail)", msg):
        predicted = [n for n in nodes if n.get("predictions")]
        if not predicted:
            return "No nodes are currently showing a trend toward threshold breach."
        lines = []
        for n in predicted:
            for feat, p in n["predictions"].items():
                eta = f", ETA ~{p['eta_ticks']} ticks" if p.get("eta_ticks") else ""
                lines.append(f"{n['node_id']}: {feat} trending upward{eta}")
        return "Predicted risks:\n" + "\n".join(lines)

    return (
        "I can answer questions like: 'cluster status', 'which nodes are critical', "
        "'tell me about gpu-node-03', 'which node is hottest', or 'any predicted failures'."
    )


def answer_query(message: str) -> str:
    """Routes to the configured LLM provider (see llm_provider.py) if one is
    set up; falls back to the fully offline rule-based assistant on any
    failure (missing/invalid credentials, network error, provider outage,
    unexpected response shape) so the chatbot is never left silent."""
    if llm_provider is not None:
        try:
            context = _build_llm_context()
            return llm_provider.complete(LLM_SYSTEM_PROMPT, f"{context}\n\nOperator question: {message}")
        except Exception as exc:
            logger.warning("LLM provider call failed (%s) - falling back to rule-based reply", exc)
    return _rule_based_reply(message)


@app.post("/api/chat")
def chat(req: ChatRequest, user: dict = Depends(auth.get_current_user)):
    return {"reply": answer_query(req.message)}