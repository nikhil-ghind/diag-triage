# diag_triage — LLM-Integrated Observability Pipeline

A log-triage service for **hardware/driver failures in scale-out clusters**. It
ingests kernel/driver diagnostic logs (dmesg, NVIDIA Xid, EDAC/ECC, NVMe, MCE,
NIC) into Elasticsearch, detects and clusters failures, triages each incident
with a **Claude agent over MCP**, and routes severity-gated, deduplicated alerts
to Slack / PagerDuty.

The design principle: **deterministic detection first, LLM last.** Cheap parsing
+ rule/rate detection runs on every line; the expensive agent runs once *per
incident*, after a fleet-wide fault has been collapsed to a single cluster.

---

## What it does

```
host forwarders ─POST /v1/ingest─► parse → index (ES) → detect → cluster
                                                              │
                                                       incident(s)
                                                              ▼
                                        Claude agent (MCP tools over ES)
                                                              ▼
                                       Triage{cause, action, blast radius}
                                                              ▼
                                  router → Slack / PagerDuty / log  (dedup+throttle)
```

Full design rationale in [`docs/architecture.md`](docs/architecture.md).

---

## Implementation details

### Ingestion (`triage/ingest/`)
- **`parser.py`** classifies each line into a subsystem (gpu/nvme/mce/memory/
  thermal/network/kernel) and severity via ordered regex rules, extracts useful
  fields (NVIDIA Xid code, PCI address), and computes a **normalized signature**
  by masking volatile tokens (`0x…`, counters, PCI ids, UUIDs, IPs → `<hex>`,
  `<n>`, …). The signature is the key everything else groups on.
- **`es_client.py`** manages index templates + ILM (hot→delete), idempotent bulk
  indexing (`event_id` = host+ts+raw hash as `_id`), incident upsert-by-
  fingerprint, and all the read queries (search, per-signature rate histogram,
  more-like-this similar incidents, per-host subsystem rollup).
- **`detector.py`** flags failures two ways — rule-based (error+ severity from a
  hardware subsystem, or any critical) and **rate-anomaly** (z-score of the
  latest bucket vs baseline, escalating a spiking "warning" to "error") — then
  clusters failures by `(subsystem, signature)` into incidents, collapsing a
  fleet-wide fault to one record.

### Agent (`triage/agent/`)
- **`triage_agent.py`** runs a bounded tool-use loop: the model investigates with
  read-only tools and terminates by calling a synthetic **`submit_triage`** tool
  whose JSON schema *is* the structured output (summary, probable cause,
  recommended action, blast radius, confidence, `event_id` citations).
- **`llm.py`** wraps the Anthropic SDK with **prompt caching** — the large stable
  system prompt + tool defs are marked `cache_control: ephemeral`, so a steady
  incident stream turns most of each request into cache reads (logged via
  `usage`). An **offline stub** runs the whole loop deterministically with no API
  key (used by tests/CI).

### MCP (`triage/mcp/`)
One set of `TOOL_SPECS` (`search_logs`, `host_health`, `signature_rate`,
`similar_incidents`) backs **both** the standalone MCP server
(`python -m triage.mcp.server`, usable from Claude Desktop or any MCP client) and
the in-process agent — identical contract either way.

### Routing (`triage/routing/`)
Severity-gated channels (critical → PagerDuty+Slack, error → Slack, low-confidence
warning → log only), with **dedup + throttling** per fingerprint so a recurring
incident pages once, not continuously. Sinks are best-effort and never raise into
the pipeline.

---

## Quick start

```bash
pip install -e ".[dev]"

# Local stack: single-node ES + service
docker compose up -d

# Create indices (or let service startup do it)
diag-triage init-indices

# Drive a synthetic fleet-wide GPU fault through the pipeline
python scripts/seed_logs.py --scenario gpu-xid --hosts 8
python scripts/seed_logs.py --scenario ecc-ramp --hosts 1   # rate-spike path

# Inspect triaged incidents
curl localhost:8080/v1/incidents | jq
```

Run the API directly: `diag-triage serve`. Run the MCP server:
`diag-triage mcp-server`. Replay a captured log file:
`diag-triage replay /var/log/kern.log --host node-042`.

### Configuration
All via `DIAG_*` env vars (see `triage/config.py`): `DIAG_ES_HOSTS`,
`DIAG_ANTHROPIC_API_KEY`, `DIAG_MODEL` (default `claude-opus-4-7`),
`DIAG_RATE_SPIKE_ZSCORE`, `DIAG_ALERT_THROTTLE_S`, `DIAG_SLACK_WEBHOOK_URL`,
`DIAG_PAGERDUTY_ROUTING_KEY`. Set `DIAG_LLM_OFFLINE=true` to run without an API
key (deterministic stub triage).

---

## Testing

```bash
pytest          # parser, detector (rate math + clustering), router, agent loop
```

Tests run fully offline — no Elasticsearch and no Anthropic key required: the
agent uses the LLM stub + a fake tool dispatch, and clustering/routing are pure
functions.

---

## Expected results

On the bundled scenarios:

| scenario | what the pipeline does |
|---|---|
| `gpu-xid` (8 hosts) | 8 hosts emit identical `Xid 79` lines → **one** critical GPU incident spanning 8 hosts → agent diagnoses "GPU off the bus, RMA-class", routes a **single** PagerDuty page + Slack, not 8 |
| `ecc-ramp` (1 host) | a burst of individually-benign correctable ECC errors trips the **rate z-score**, escalates warning→error, opens a memory incident → agent recommends pre-emptive drain before an uncorrectable error |
| `nvme-reset` | NVMe timeout/reset lines cluster into one storage incident, Slack alert |
| `mce` | uncorrected machine-check → immediate critical incident + page |

Key properties the pipeline guarantees:
- **Fan-in:** N identical failures across hosts → 1 incident → 1 alert.
- **Idempotent ingest:** replaying the same lines does not double-count
  (`event_id` as ES `_id`).
- **Throttled:** recurring incidents page once per `alert_throttle_s`.
- **Cited:** every agent triage carries the `event_id`s it used as evidence, and
  prompt-cache read/write token counts are logged per incident.

---

## License

Apache-2.0.
