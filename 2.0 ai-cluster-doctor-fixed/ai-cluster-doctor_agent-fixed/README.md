# AI Cluster Doctor

Predictive GPU cluster health monitoring: a Host that collects telemetry from a
built-in simulator and/or real Agents, runs anomaly detection and failure
prediction on it, and surfaces it through a live dashboard and chat assistant.

---

## 1. Project Overview

AI Cluster Doctor started as a single-machine prototype (a FastAPI backend, a
telemetry simulator, and a static HTML dashboard) and has grown into a
Host + Agent monitoring platform:

- A **Host** (FastAPI) that ingests telemetry from multiple sources, runs the
  ML pipeline, stores device identity in SQLite, and serves a JWT-authenticated
  REST API.
- **Agents** that can run on real machines, auto-discover the Host on the LAN,
  and report telemetry to it.
- A **React dashboard** (replacing the original static HTML page) for viewing
  cluster health and talking to the chatbot.
- Pluggable **telemetry providers** (simulator, Agents, Prometheus, DCGM) that
  all feed the same unmodified ML pipeline.
- Docker and Kubernetes deployment manifests.

This README documents exactly what's implemented - nothing here describes a
planned-but-unbuilt feature except where explicitly marked under
[Future Roadmap](#future-roadmap).

---

## 2. Architecture

```
                    ┌─────────────────────────┐
   Agents  ───HTTP──▶   Host (FastAPI)         │◀──── React dashboard (browser)
 (real PCs)   +key   │  - TelemetryProvider(s)  │      - JWT login
                     │  - IsolationForest        │      - polls /api/nodes every 3s
Simulator ───────────▶  - XGBoost long-term      │      - chat widget
(built-in,           │  - JWT auth + RBAC        │
 always on)          │  - SQLite (devices, users)│
                     │  - LLM chatbot (optional) │
                     └───────────┬──────────────┘
                                 │
                    UDP broadcast (LAN discovery, port 47110)
                                 │
                          more Agents...
```

The Host is the only component that runs the ML pipeline, holds
authentication state, or talks to SQLite. Agents only collect and forward
telemetry - they never run any AI/ML themselves.

---

## 3. Host

The Host (`backend/`) is a FastAPI application. Responsibilities:

- Runs the built-in simulator continuously (a background asyncio loop ticking
  every 2 seconds).
- Accepts telemetry pushed by Agents via `POST /api/agent/telemetry`.
- Responds to LAN discovery broadcasts (UDP port 47110) so Agents can find it
  without manual IP configuration.
- Runs the ML pipeline (Isolation Forest anomaly detection, rule-based health
  scoring, linear short-term trend prediction, XGBoost long-term prediction)
  on every node's telemetry history, on-demand per request.
- Stores device identity (hostnames, nicknames, first/last seen, soft-delete)
  and user accounts in SQLite.
- Issues and verifies JWTs, enforces role-based access control.
- Answers chatbot questions - via a configured LLM provider if one is set up,
  otherwise via a fully offline rule-based assistant.

## 4. Agent

The Agent (`agent/`) is a small standalone Python program meant to run on a
real machine you want monitored:

- **`agent_core.py`** - platform-independent core logic:
  - Generates a UUID **device ID** once and persists it to disk
    (`%PROGRAMDATA%\ClusterDoctorAgent\device_id.txt` on Windows, or
    `/etc/cluster-doctor-agent/device_id.txt` on Linux/macOS, falling back to
    a user-writable path if that's not writable), so reinstalling or
    restarting the Agent doesn't register as a new device.
  - **Auto-discovers** the Host via a UDP broadcast on port 47110 - no manual
    IP configuration needed (unless you set `CLUSTER_DOCTOR_HOST_URL`
    explicitly, which skips discovery entirely).
  - Collects a telemetry reading via `psutil`, mapped onto the Host's
    5-key contract (see [TelemetryProvider](#5-telemetryprovider) below).
    Fields with no reliable cross-platform source (`power_watts`,
    `fan_speed_pct` on a non-GPU machine) report `0.0` - an honest
    placeholder, not a fabricated value. **Known consequence:** this can
    trigger a false "critical fan failure" root cause from the existing
    GPU-oriented ML thresholds on non-GPU machines - see
    [Troubleshooting](#troubleshooting).
  - POSTs the reading to the Host every 5 seconds, with automatic
    reconnect/backoff (2s -> 5s -> 10s -> 30s -> 60s) on any failure, and
    forces rediscovery if the Host becomes unreachable.
- **`windows_service.py`** - wraps `AgentRunner` as a native Windows Service
  (via `pywin32`) so it starts automatically with Windows and runs silently in
  the background. **This has not been tested on a real Windows machine** -
  only `agent_core.py`'s cross-platform logic has been verified end-to-end.

## 5. TelemetryProvider

`backend/providers.py` defines the abstraction every telemetry source
implements, so `main.py` and `ml.py` never need to know or care where data
actually comes from:

```python
class TelemetryProvider(ABC):
    def tick(self) -> None: ...
    def get_node_ids(self) -> List[str]: ...
    def node_exists(self, node_id: str) -> bool: ...
    def get_node_info(self, node_id: str) -> Dict: ...
    def get_history(self, node_id: str, limit: int = 300) -> List[dict]: ...
```

Every reading, from every provider, is a dict with the same frozen 5-key
contract:

```
temperature_c, gpu_utilization_pct, memory_utilization_pct, power_watts, fan_speed_pct
```

`CompositeProvider` combines multiple providers into one, routing each call to
whichever sub-provider owns that node ID. In `main.py`, the Host currently
runs:

```python
sim: TelemetryProvider = CompositeProvider([SimulatorProvider(), agent_provider])
```

`PrometheusProvider` and `DCGMProvider` also exist and correctly implement the
interface (see below), but are **not** included in this list by default.

## 6. Simulator

`backend/simulator.py` (unchanged since the original prototype) generates
telemetry for 8 fixed nodes (`gpu-node-01` … `gpu-node-08`), each with a
random GPU model and baseline. Every 2-second tick, there's a small chance
(0.15%) a node starts "degrading" - metrics drift toward critical over
several minutes, with occasional auto-recovery and transient spikes. This
runs continuously and always shows up in `/api/nodes` alongside any real
Agents or other providers.

`SimulatorProvider` (`backend/providers.py`) is a thin, behavior-preserving
adapter around `ClusterSimulator` that implements `TelemetryProvider`.

## 7. Prometheus

`backend/prometheus_provider.py` - `PrometheusProvider` pulls the latest DCGM
metrics for every GPU a Prometheus server knows about, via one instant
PromQL query per metric (`/api/v1/query`), and buffers results locally
per node (same shape as every other provider).

**Metric mapping:**

| DCGM Prometheus metric | Our field |
|---|---|
| `DCGM_FI_DEV_GPU_TEMP` | `temperature_c` |
| `DCGM_FI_DEV_GPU_UTIL` | `gpu_utilization_pct` |
| `DCGM_FI_DEV_MEM_COPY_UTIL` | `memory_utilization_pct` |
| `DCGM_FI_DEV_POWER_USAGE` | `power_watts` |
| *(no standard field)* | `fan_speed_pct` always `0.0` - most datacenter GPUs have no per-GPU fan sensor |

Configured via `PROMETHEUS_URL` (default `http://localhost:9090`). Verified
against a mocked Prometheus HTTP API; **not wired into the Host's active
provider list by default** - see [Configuration](#configuration) for how
to add it.

## 8. DCGM

`backend/dcgm_provider.py` - `DCGMProvider` scrapes one or more
`dcgm-exporter` `/metrics` endpoints directly (Prometheus text-exposition
format), bypassing a Prometheus server entirely. Same metric mapping as
above. Configured via `DCGM_EXPORTER_TARGETS` (comma-separated `host:port`
list). Verified against a mocked dcgm-exporter text endpoint; also not wired
in by default.

## 9. SQLite

`backend/db.py` owns two things:

- **Device registry** (`devices` table): device ID, original hostname,
  user-assigned nickname, source provider, first/last seen, soft-delete flag.
  Survives Host restarts.
- **Users** (`users` table): username, PBKDF2-HMAC-SHA256 password hash,
  role (`admin`/`viewer`), created-at.
- **Settings** (`settings` table): a generic key-value store, currently used
  to persist the auto-generated JWT signing secret and Agent API key across
  restarts.

**Not yet persisted in SQLite:** telemetry history itself, which still lives
in each provider's in-memory buffer and is lost on restart - only device
*identity* and auth state survive restarts.

The database file location is `backend/cluster_doctor.db` by default,
overridable via `CLUSTER_DOCTOR_DB_PATH` (used by the Docker/Kubernetes
deployments to place it on a persistent volume).

## 10. JWT

`backend/auth.py` issues and verifies JSON Web Tokens (HS256, 24-hour
expiry). `POST /api/auth/login` exchanges a username/password for a token:

```json
{"username": "admin", "password": "..."}
-> {"access_token": "...", "token_type": "bearer", "role": "admin"}
```

Every other dashboard/API endpoint requires `Authorization: Bearer <token>`.
The JWT signing secret is auto-generated on first run and persisted in
SQLite (overridable via `JWT_SECRET`).

On first startup with no users in the database, the Host creates an initial
`admin` account with a random password, **printed once to the startup logs**
(`ADMIN_USERNAME`/`ADMIN_PASSWORD` env vars let you pin it instead).

Agents use a **separate** credential - a shared API key sent via the
`X-API-Key` header on `POST /api/agent/telemetry` - not a JWT, since Agents
are machines, not logged-in users. This key is also auto-generated and
persisted (overridable via `CLUSTER_DOCTOR_AGENT_API_KEY`), and printed to
the Host's startup logs.

## 11. RBAC

Two roles: **`admin`** and **`viewer`**.

| Action | `viewer` | `admin` |
|---|---|---|
| View nodes, alerts, history, use chat | Yes | Yes |
| Rename a device (`PATCH /api/nodes/{id}/nickname`) | No (403) | Yes |
| Delete/hide a device (`DELETE /api/nodes/{id}`) | No (403) | Yes |

There is currently no user-management API to create additional users -
new accounts must be created directly via `db.create_user()` (e.g. from a
Python shell) until such an endpoint exists.

## 12. HTTPS

The Host has no built-in HTTPS termination of its own; it relies on
uvicorn's standard `--ssl-keyfile`/`--ssl-certfile` flags. `backend/generate_dev_cert.sh`
generates a self-signed certificate for local testing:

```bash
cd backend
./generate_dev_cert.sh
uvicorn main:app --host 0.0.0.0 --port 8000 --ssl-keyfile key.pem --ssl-certfile cert.pem
```

For production, use a certificate from a real CA instead of the self-signed
one. The LAN-discovery protocol is scheme-aware: set `CLUSTER_DOCTOR_SCHEME=https`
on the Host so discovery replies tell Agents to connect over HTTPS; Agents
verify TLS certificates by default (`CLUSTER_DOCTOR_VERIFY_TLS=false` disables
verification, for self-signed certs in local testing only).

In the Kubernetes deployment, TLS is expected to terminate at the Ingress
(commented-out `tls:` block in `k8s/ingress.yaml`, ready for a cert-manager
Secret) rather than in the Host process itself.

## 13. LLM

`backend/llm_provider.py` defines one interface, `LLMProvider.complete(system_prompt, user_message)`,
implemented by three backends, selected via `CLUSTER_DOCTOR_LLM_PROVIDER`:

| Value | Backend | Required env vars |
|---|---|---|
| `openai` | OpenAI Chat Completions | `OPENAI_API_KEY`, optional `OPENAI_MODEL` (default `gpt-4o-mini`) |
| `anthropic` | Anthropic Messages API | `ANTHROPIC_API_KEY`, optional `ANTHROPIC_MODEL` (default `claude-3-5-haiku-20241022`) |
| `ollama` | Local Ollama server | optional `OLLAMA_HOST` (default `http://localhost:11434`), `OLLAMA_MODEL` (default `llama3`) |
| *(unset)* | none - offline only | - |

If no provider is configured, or the configured one fails to initialize
(missing key) or fails at call time (network error, invalid key, provider
outage, unexpected response), the Host **automatically falls back** to the
original fully offline, rule-based chatbot - verified live, including a real
401 from Anthropic's API with an invalid test key correctly triggering the
fallback path.

The LLM is given a text snapshot of current cluster telemetry (node IDs,
health scores, root causes, latest metrics, predictions) as context, and
instructed not to invent data outside it.

**Not verified:** an actual successful completion from OpenAI or Anthropic -
no real API key was available during development, so only the
construction-failure, call-failure, and fallback paths were tested end to
end.

## 14. Isolation Forest

`backend/ml.py::detect_anomaly()` fits a fresh `sklearn.ensemble.IsolationForest`
(100 estimators, `contamination=0.08`) on each node's own recent history
(minimum 40 samples), on every request. The anomaly score is normalized into
a 0-1 range and factored into the health score (`anomaly_score * 25` points
deducted). This is unchanged from the original prototype throughout every
phase of this project.

`compute_health()` combines this with:

1. **Threshold-based deductions** - fixed point deductions when a metric
   crosses warn/critical thresholds (temperature, memory, power, fan speed).
2. The Isolation Forest anomaly score above.
3. **Short-term trend prediction** (`predict_trend()`, unchanged from the
   original prototype) - linear regression (`np.polyfit`) over the last 60
   readings, gated by a minimum slope, projecting an ETA to threshold breach
   within a 60-tick (~120s) lookahead.
4. **Long-term trend prediction** (`predict_long_term()`, XGBoost) - see next
   section.

## 15. XGBoost

`backend/ml.py::predict_long_term()` adds a second, independent failure
predictor alongside the original linear one, for `temperature_c`,
`memory_utilization_pct`, and `fan_speed_pct`, at two horizons: 60 and 150
ticks ahead (~2 and ~5 minutes).

For each feature/horizon, it trains a small `XGBRegressor` (30 estimators,
max depth 3) on sliding windows of the node's own history, predicting the
**delta** over the horizon (not the absolute future value - tree-based
models can't extrapolate past the range of values they were trained on, so
predicting a delta and adding it to the current reading is what makes
genuine extrapolation possible; this was a real bug found and fixed during
development). Needs at least 20 valid training pairs before it will produce
a prediction for a given horizon - so it activates later than the short-term
predictor (roughly 3-6 minutes of history, depending on horizon), by design.

Both predictors run independently and appear as separate response fields:
`predictions` (short-term/linear) and `long_term_predictions` (XGBoost).
Verified to coexist correctly, including a case where the long-term
predictor catches a slow trend the short-term one's 60-tick lookahead
misses.

## 16. Action Planner and Health Score Audit

Three additive backend modules and three presentational frontend components
that make the existing health score explainable and actionable, without
changing `ml.py`'s scoring logic itself.

**Score breakdown** (`backend/score_audit.py::audit_score()`) - re-derives
the same per-metric point deductions `compute_health()` already computes
(reusing `ml.THRESHOLDS`/`ml._threshold_deduction`, not a second set of
numbers) into a list of `{metric, penalty, reason}` entries, plus the
Isolation Forest anomaly blend as its own line item. Rendered by
`ScoreBreakdown.jsx`.

**Action priority** (`backend/action_planner.py::plan_action()`) - rule-based
only, no model. Looks at whichever metric caused the largest deduction and
maps it to one concrete next step, with a priority:

| Priority | Meaning |
|---|---|
| `NOW` | Critical threshold breached - act immediately |
| `TODAY` | Warning-level breach - address within the day |
| `LATER` | Minor/anomaly-only signal - monitor |

Returns `None` for a healthy node (score >= 85) - nothing to plan. Rendered
by `ActionPlanner.jsx`.

**Action logging** (`backend/action_logger.py`) - `save_action()` /
`get_actions()` persist each planned action (node, action text, priority,
reason, before/after health score, result) to a dedicated `actions` table in
the same SQLite file `db.py` already uses (own connection, `db.py` itself is
untouched). In the current prototype wiring, `main.py` calls
`demo_resolve_latest()` right after logging an action, which fills in a
fixed demo outcome (`after_score=75`, `result="Improved"`) so the History
panel has something to show live in a demo without wiring real before/after
health-check polling yet - see [Future Roadmap](#future-roadmap). Rendered
by `ActionHistory.jsx`.

### Updated architecture flow

```
telemetry reading
      |
      v
ml.compute_health()  (unchanged: score, status, root_cause, anomaly, predictions)
      |
      +--> score_audit.audit_score()   -> score_audit   {health_score, score_breakdown[]}
      |
      +--> action_planner.plan_action() -> planned_action {action, priority, reason} | null
      |         |
      |         v
      |    action_logger.save_action() -> actions table (SQLite)
      |         |
      |         v
      |    action_logger.demo_resolve_latest()  (prototype-only demo outcome)
      |
      v
_node_summary() response  ->  GET /api/nodes, /api/nodes/{id}, /api/alerts
      |
      v
NodeCard.jsx  ->  ScoreBreakdown / ActionPlanner / ActionHistory
```

`GET /api/nodes/{node_id}/actions` also exposes the same action history
directly, independent of a node summary fetch.

### New files

```
backend/
 - score_audit.py       # audit_score(latest, anomaly_score) -> score breakdown
 - action_planner.py    # plan_action(root_cause, latest, health_score) -> next action
 - action_logger.py     # save_action() / get_actions() / demo_resolve_latest()

frontend/src/components/
 - ScoreBreakdown.jsx   # renders node.score_audit.score_breakdown
 - ActionPlanner.jsx    # renders node.planned_action
 - ActionHistory.jsx    # renders node.action_history
```

None of the above required changes to `ml.py`, `db.py`, or any existing
frontend file other than importing/rendering the three new components inside
`NodeCard.jsx`.

### Running instructions

Same processes as [Development](#development) above - no separate service
to start for this feature, it's part of the existing Host/frontend/Agent:

```bash
# Backend
cd backend
uvicorn main:app --reload --port 8000

# Frontend
cd frontend
npm run dev

# Agent (optional)
cd agent
python agent_core.py
```

### Hackathon demo workflow

1. Start the Host and frontend as above, sign in.
2. Wait for (or wait out) a simulator node drifting into `warning` or
   `critical` - or point a real Agent at a loaded machine.
3. Click that node's card: the existing health score/root cause is now
   followed by a **Score Breakdown** table showing exactly which metrics
   cost how many points, an **Action Planner** card with a concrete
   NOW/TODAY/LATER recommendation, and an **Action History** panel showing
   that action logged with a demo before/after score and an "Improved"
   result.
4. Ask the chat widget about the same node to show the existing root-cause
   explanation lines up with the new breakdown.

---

## API

All endpoints are served by the Host (`backend/main.py`). Interactive docs
are available at `/docs` once running.

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/auth/login` | none | `{"username","password"}` -> JWT access token |
| `GET` | `/api/nodes` | JWT | All visible nodes' current status. Optional `?search=` filters by node ID, hostname, or nickname |
| `GET` | `/api/nodes/{node_id}` | JWT | Single node's current status |
| `GET` | `/api/nodes/{node_id}/history?limit=120` | JWT | Raw telemetry history |
| `GET` | `/api/nodes/{node_id}/actions` | JWT | Logged actions for one node (score audit / action planner history) |
| `PATCH` | `/api/nodes/{node_id}/nickname` | JWT + `admin` | Rename a device: `{"nickname": "..."}` |
| `DELETE` | `/api/nodes/{node_id}` | JWT + `admin` | Soft-delete (hide) a device; reappears if seen again |
| `GET` | `/api/alerts` | JWT | Nodes currently in `warning`/`critical` state |
| `POST` | `/api/chat` | JWT | `{"message": "..."}` -> `{"reply": "..."}` |
| `POST` | `/api/agent/telemetry` | `X-API-Key` | Agents push a reading: `{"device_id","hostname","reading"}` |

A node's status response includes: `node_id`, `name`, `hostname`, `nickname`,
`gpu_model`, `first_seen`, `last_seen`, `latest` (raw reading), `health_score`,
`status` (`healthy`/`warning`/`critical`), `root_cause`, `anomaly`,
`predictions`, `long_term_predictions`, `score_audit`, `planned_action`,
`action_history`.

---

## Installation

### Prerequisites
- Python 3.12+
- Node.js 20+ (for the frontend)
- (Optional) Docker + Docker Compose
- (Optional) A Kubernetes cluster + `kubectl`

### Clone and set up

```bash
git clone <this repo>
cd ai-cluster-doctor
```

---

## Development

### Backend (Host)

```bash
cd backend
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

On first run, the Host prints an initial admin username/password and an
Agent API key to the console - save these, they're each shown only once.

### Frontend

```bash
cd frontend
cp .env.example .env   # adjust VITE_API_BASE if needed
npm install
npm run dev
```

Open the printed local URL (default `http://localhost:5173`), and sign in
with the admin credentials from the Host's startup logs.

### Agent (optional, to report a real machine's telemetry)

```bash
cd agent
pip install -r requirements.txt
export CLUSTER_DOCTOR_API_KEY=<agent key from Host startup logs>
python agent_core.py
```

The Agent auto-discovers the Host via LAN broadcast; set
`CLUSTER_DOCTOR_HOST_URL` to skip discovery and connect directly.

### Docker (dev mode - hot reload)

```bash
docker compose up --build
```

Backend on `:8000` with `--reload` and bind-mounted source; frontend on
`:5173` via the Vite dev server with bind-mounted source.

---

## Production

### Backend

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

(Single worker deliberately - SQLite doesn't handle concurrent multi-process
writers as gracefully as a client-server database.)

### Frontend

```bash
cd frontend
npm install
npm run build
npm run preview   # or serve dist/ with any static file server / nginx
```

`VITE_API_BASE` is baked in at **build time** - it must be a URL reachable
from the end user's browser.

### Docker (production)

```bash
docker compose -f docker-compose.yml up --build
```

Backend on `:8000`, frontend built and served via nginx on `:80`. An
optional containerized Agent (for demos, not real Windows hardware) is
available via `docker compose --profile demo up --build`.

**Note:** the Docker setup has been reviewed and validated for syntax and
internal consistency, but Docker itself was not available to actually build
and run these images during development - test this yourself before relying
on it in production.

---

## Docker

Per-service multi-stage `Dockerfile`s (`dev`/`prod` targets) exist for the
backend, frontend, and agent. Compose files:

- **`docker-compose.yml`** - production base (no bind mounts, no reload).
- **`docker-compose.override.yml`** - development overrides, auto-merged by
  plain `docker compose up` (hot reload, bind-mounted source, Vite dev
  server). Run `docker compose -f docker-compose.yml up` alone to skip it.

Key environment variables (see `.env.example` at the repo root):

| Variable | Used by | Purpose |
|---|---|---|
| `VITE_API_BASE` | frontend build | Public URL the browser will call - **not** a Docker-internal hostname |
| `AGENT_API_KEY` | `agent-demo` profile | Must match the Host's own agent API key |

SQLite persists via a named volume (`host_data`) mounted at `/app/data`,
with `CLUSTER_DOCTOR_DB_PATH=/app/data/cluster_doctor.db` set in the
compose environment.

LAN auto-discovery (UDP broadcast) does not reliably work across Docker's
default bridge network to reach *physical* devices outside the Docker host -
use `network_mode: host` (Linux) or point Agents at `CLUSTER_DOCTOR_HOST_URL`
explicitly instead.

---

## Kubernetes

Manifests live in `k8s/`, covering all of: ConfigMap, Secret, PersistentVolume
(+PersistentVolumeClaim), Deployment, Service, Ingress.

```bash
kubectl apply -f k8s/persistent-volume.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml        # edit placeholders first, or generate via kubectl create secret (see file)
kubectl apply -f k8s/host-deployment.yaml -f k8s/host-service.yaml
kubectl apply -f k8s/frontend-deployment.yaml -f k8s/frontend-service.yaml
kubectl apply -f k8s/ingress.yaml       # requires an ingress-nginx controller
```

Notable design decisions (all commented in the manifests themselves):

- `host` Deployment is pinned to **1 replica** - same SQLite concurrency
  reasoning as the Docker production stage.
- The `PersistentVolume` uses `hostPath` static provisioning, appropriate for
  local/on-prem clusters (kind/minikube/k3s); swap for your cloud's
  StorageClass + dynamic provisioning in a real managed cluster.
- The Ingress uses a **single hostname** with path-based routing (`/api/*` ->
  Host, `/` -> frontend), since the frontend's `api.js` already prepends
  `/api/...` to every call - `VITE_API_BASE` just needs to be the bare
  origin.
- Readiness/liveness probes use FastAPI's built-in `/openapi.json` (already
  exists, unauthenticated) rather than a dedicated health endpoint, since
  adding one was out of scope for the phase that created these manifests.
- No Service exposes the UDP discovery port - broadcast doesn't traverse
  cluster networking to reach real external devices regardless of Service
  config; point Agents outside the cluster at the Ingress hostname via
  `CLUSTER_DOCTOR_HOST_URL`.

**Note:** these manifests were validated for YAML syntax and internal
consistency (labels/selectors/ports/names all cross-checked
programmatically) but not actually applied to a real cluster - no `kubectl`
or cluster was available during development.

---

## Running

Once the Host and frontend are both running (any of the methods above):

1. Open the frontend URL in a browser.
2. Sign in with the admin username/password printed in the Host's startup
   logs (or an account you created).
3. The dashboard polls `/api/nodes` every 3 seconds; the 8 simulator nodes
   appear immediately, and any real Agents (or Prometheus/DCGM-sourced
   nodes, if wired in) appear alongside them as they report in.
4. Use the chat widget (bottom-right) to ask about cluster status, or click
   any node card to ask about it directly.

---

## Configuration

All configuration is via environment variables (no config files).

### Host

| Variable | Default | Purpose |
|---|---|---|
| `CLUSTER_DOCTOR_PORT` | `8000` | Must match the port uvicorn is actually run on (used for discovery replies) |
| `CLUSTER_DOCTOR_SCHEME` | `http` | `http` or `https`, advertised in discovery replies |
| `CLUSTER_DOCTOR_DB_PATH` | `<backend dir>/cluster_doctor.db` | SQLite file location |
| `JWT_SECRET` | auto-generated, persisted in SQLite | Pin explicitly across multiple Host instances sharing a DB |
| `CLUSTER_DOCTOR_AGENT_API_KEY` | auto-generated, persisted in SQLite | Shared secret Agents must send |
| `ADMIN_USERNAME` | `admin` | Initial admin account username (first run only) |
| `ADMIN_PASSWORD` | random, printed once | Initial admin account password (first run only) |
| `CLUSTER_DOCTOR_LLM_PROVIDER` | unset (offline only) | `openai` \| `anthropic` \| `ollama` |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | - | Required if provider is `openai` |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | - | Required if provider is `anthropic` |
| `OLLAMA_HOST` / `OLLAMA_MODEL` | `http://localhost:11434` / `llama3` | Used if provider is `ollama` |
| `PROMETHEUS_URL` | `http://localhost:9090` | Used only if you wire in `PrometheusProvider` yourself |
| `DCGM_EXPORTER_TARGETS` | unset | Comma-separated `host:port` list, used only if you wire in `DCGMProvider` yourself |

### Agent

| Variable | Default | Purpose |
|---|---|---|
| `CLUSTER_DOCTOR_API_KEY` | unset | Must match the Host's agent API key |
| `CLUSTER_DOCTOR_HOST_URL` | unset (auto-discover) | Skip LAN discovery, connect directly |
| `CLUSTER_DOCTOR_DISCOVERY_ADDR` | `255.255.255.255` | Override for testing without a real broadcast domain |
| `CLUSTER_DOCTOR_VERIFY_TLS` | `true` | Set `false` to accept self-signed certs (local testing only) |

### Frontend

| Variable | Default | Purpose |
|---|---|---|
| `VITE_API_BASE` | `http://127.0.0.1:8000` | Host URL - baked in at build time |

### Wiring in Prometheus/DCGM providers

Not active by default. To enable, edit `backend/main.py` where `sim` is
constructed:

```python
from prometheus_provider import PrometheusProvider
from dcgm_provider import DCGMProvider

sim: TelemetryProvider = CompositeProvider([
    SimulatorProvider(),
    agent_provider,
    PrometheusProvider(),   # or DCGMProvider()
])
```

---

## Folder Structure

```
ai-cluster-doctor/
|-- backend/                     # Host (FastAPI)
|   |-- main.py                   # API routes, auth wiring, chatbot routing, lifespan
|   |-- providers.py               # TelemetryProvider ABC, SimulatorProvider, CompositeProvider
|   |-- simulator.py               # ClusterSimulator (unchanged from original prototype)
|   |-- agent_provider.py          # AgentProvider - buffers telemetry pushed by Agents
|   |-- prometheus_provider.py     # PrometheusProvider (not wired in by default)
|   |-- dcgm_provider.py           # DCGMProvider (not wired in by default)
|   |-- ml.py                      # IsolationForest, health scoring, linear + XGBoost prediction
|   |-- score_audit.py             # Score breakdown explainability (reuses ml.THRESHOLDS)
|   |-- action_planner.py          # Rule-based NOW/TODAY/LATER action recommendation
|   |-- action_logger.py           # Action history persistence (own table, same SQLite file as db.py)
|   |-- db.py                      # SQLite: devices, users, settings
|   |-- auth.py                    # JWT, RBAC, agent API key
|   |-- discovery.py               # UDP LAN discovery responder
|   |-- llm_provider.py            # LLMProvider abstraction: OpenAI/Anthropic/Ollama
|   |-- requirements.txt
|   |-- Dockerfile                 # dev/prod multi-stage
|   |-- generate_dev_cert.sh       # self-signed TLS cert for local HTTPS testing
|   `-- .dockerignore
|-- agent/                       # Agent (runs on monitored machines)
|   |-- agent_core.py              # platform-independent core logic
|   |-- windows_service.py         # Windows Service wrapper (untested on real Windows)
|   |-- requirements.txt
|   |-- Dockerfile                 # for demoing Host<->Agent flow without real Windows hardware
|   `-- .dockerignore
|-- frontend/                    # React (Vite) dashboard
|   |-- src/
|   |   |-- main.jsx / App.jsx
|   |   |-- api.js                 # API client, JWT token storage
|   |   |-- styles.css             # preserves the original dashboard's palette/layout
|   |   `-- components/
|   |       |-- LoginScreen.jsx
|   |       |-- Dashboard.jsx
|   |       |-- EcgStrip.jsx
|   |       |-- SummaryPills.jsx
|   |       |-- AlertBar.jsx
|   |       |-- NodeGrid.jsx / NodeCard.jsx
|   |       |-- ScoreBreakdown.jsx     # Health score breakdown table (node.score_audit)
|   |       |-- ActionPlanner.jsx      # Recommended action card (node.planned_action)
|   |       |-- ActionHistory.jsx      # Logged action history (node.action_history)
|   |       `-- ChatWidget.jsx
|   |-- package.json / vite.config.js / index.html
|   |-- Dockerfile                 # dev/prod multi-stage (prod serves via nginx)
|   |-- nginx.conf
|   `-- .dockerignore
|-- k8s/                          # Kubernetes manifests
|   |-- configmap.yaml / secret.yaml
|   |-- persistent-volume.yaml    # PersistentVolume + PersistentVolumeClaim
|   |-- host-deployment.yaml / host-service.yaml
|   |-- frontend-deployment.yaml / frontend-service.yaml
|   `-- ingress.yaml
|-- docker-compose.yml            # production
|-- docker-compose.override.yml   # development (auto-merged)
|-- .env.example
`-- README.md
```

---

## REST APIs

See [API](#api) above for the full endpoint table. All request/response
bodies are JSON; interactive Swagger docs are auto-served at `/docs` and
`/openapi.json`.

---

## Deployment

Three supported paths, in increasing order of production-readiness:

1. **Bare processes** - `uvicorn` + `npm run build`/serve, as described under
   [Production](#production). Simplest, no containerization.
2. **Docker Compose** - `docker-compose.yml` (prod) /
   `docker-compose.override.yml` (dev). See [Docker](#docker).
3. **Kubernetes** - manifests in `k8s/`. See [Kubernetes](#kubernetes).

In all three, the Host is a single stateful process (SQLite) and the
frontend is stateless and horizontally scalable. Agents are deployed
independently, one per monitored machine.

---

## Future Roadmap

Explicitly **not** implemented - listed here so it's clear these are ideas,
not existing features:

- Persisting telemetry history itself in SQLite (currently only device
  identity and auth state survive a Host restart).
- A user-management API (creating/deleting/updating accounts currently
  requires calling `db.create_user()` directly).
- Wiring `PrometheusProvider`/`DCGMProvider` into the Host's default
  provider list (they exist and work, but require a manual code edit to
  activate - see [Configuration](#configuration)).
- A dedicated `/health` endpoint (Kubernetes probes currently reuse
  FastAPI's built-in `/openapi.json`).
- Real-machine (non-GPU) telemetry thresholds - Agent-reported PCs without a
  GPU currently get scored against the same GPU-oriented thresholds as
  simulator/DCGM nodes, causing some false positives (see
  [Troubleshooting](#troubleshooting)).
- Real action outcomes for the Action Planner/Logger - `action_logger.demo_resolve_latest()`
  currently fills in a fixed demo `after_score`/`Improved` result rather than
  re-checking the node's actual health score after some real elapsed time;
  wiring that up is a follow-up, not yet implemented.
- HTTPS termination inside the Host process is possible via uvicorn flags,
  but there's no built-in reverse proxy/cert-renewal automation - that's
  left to your deployment environment (e.g. cert-manager at the Kubernetes
  Ingress).

---

## Troubleshooting

**"No nodes are currently in a critical state" right after starting the
Host.** Expected - the simulator's degradation events are random (0.15%
chance per 2s tick per node). It can take several minutes of continuous
running before any node reaches `warning` or `critical`.

**Agent-reported (or DCGM-sourced) devices show a false "critical fan
failure" or similar root cause.** `fan_speed_pct` and sometimes
`power_watts` are honestly reported as `0.0` when there's no real sensor
data available (most non-GPU machines, and most DCGM setups, don't expose
per-device fan telemetry) - the existing GPU-oriented threshold rules in
`ml.py` interpret `0.0` fan speed as a hardware failure. This is a known,
documented limitation, not a bug in telemetry collection.

**Frontend shows 401 errors / keeps bouncing to the login screen.** Your
JWT expired (24h) or the Host restarted with a new auto-generated
`JWT_SECRET` (pin it via the env var if you need tokens to survive Host
restarts) - sign in again.

**Agent can't find the Host ("Host discovery timed out").** LAN broadcast
discovery doesn't cross Docker's default bridge network or Kubernetes
cluster networking to reach real external devices - set
`CLUSTER_DOCTOR_HOST_URL` on the Agent explicitly instead of relying on
discovery in those environments. On bare metal, confirm the Agent and Host
are on the same broadcast domain (same LAN segment/VLAN).

**Agent telemetry POST returns 401.** `CLUSTER_DOCTOR_API_KEY` on the Agent
doesn't match the Host's agent API key - check the Host's startup logs (or
`CLUSTER_DOCTOR_AGENT_API_KEY` if you pinned it) and update the Agent's
environment to match.

**Chatbot gives a rule-based reply even though I configured an LLM
provider.** Check the Host's logs for a warning starting with "Failed to
initialize LLM provider" (bad/missing API key) or "LLM provider call
failed" (network error, invalid key, provider outage) - the Host always
falls back to the offline assistant rather than returning an error, by
design.

**`docker compose up` fails to bind port 80 in dev mode.** Known Compose
behavior: list-type fields like `ports` are concatenated (not replaced)
across `docker-compose.yml` and `docker-compose.override.yml`, so the base
file's `80:80` mapping is still present in dev mode even though nothing
listens on it there. Free up host port 80 or ignore the unused mapping.

---

## License

No license file is currently included in this project.
