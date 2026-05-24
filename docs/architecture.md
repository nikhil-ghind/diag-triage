# diag_triage architecture

A log-triage service for hardware/driver failures in scale-out clusters. It
turns a firehose of kernel/driver diagnostic logs into a small number of
*triaged incidents with recommended actions*, by combining cheap deterministic
detection with an LLM agent that only runs on the rare events worth its cost.

## Data flow

```
 host log forwarders                 ┌──────────── Elasticsearch ────────────┐
 (dmesg/journald/Filebeat)           │  diag-logs (events, ILM hot->delete)   │
        │  POST /v1/ingest           │  diag-incidents (upserted by fingerprint)│
        ▼                            └────────────────────────────────────────┘
 ┌─────────────┐  parse   ┌──────────┐  bulk index        ▲          ▲
 │  parser     │─────────►│ pipeline │────────────────────┘          │
 │ (signature) │          └────┬─────┘                               │ read
 └─────────────┘               │ detect + cluster                    │
                               ▼                                      │
                         ┌───────────┐  incidents   ┌──────────────┐ │
                         │ detector  │─────────────►│ triage agent │─┘ (MCP tools)
                         │ rules +   │              │ Claude +     │
                         │ rate z    │              │ tool loop    │
                         └───────────┘              └──────┬───────┘
                                                           │ Triage
                                                           ▼
                                                    ┌─────────────┐
                                                    │ alert router│──► Slack / PagerDuty / log
                                                    │ dedup+throttle│
                                                    └─────────────┘
```

## Why this shape

**Deterministic first, LLM last.** Parsing, signature normalization, rule-based
detection and rate-anomaly scoring are all cheap and run on every line. The LLM
agent — the only expensive component — runs *per incident*, not per line. A GPU
fault that floods 500 hosts collapses to **one** incident (clustering by
`(subsystem, signature)`), so the agent is invoked once, not 500 times.

**Signature normalization is the load-bearing trick.** `parser.normalize_signature`
masks volatile tokens (hex addresses, counters, PCI ids, UUIDs, IPs) so that
physically-distinct-but-logically-identical failures share a signature. That
single key drives clustering, rate-anomaly aggregation, dedup, and throttling.

**The agent investigates, it doesn't guess.** The triage agent is given
read-only MCP tools over the same Elasticsearch store the pipeline writes to:
`search_logs`, `host_health`, `signature_rate`, `similar_incidents`. It runs a
bounded tool-use loop and terminates by calling a synthetic `submit_triage`
tool whose JSON schema *is* the structured output contract — summary, probable
cause, recommended action, blast radius, confidence, and the `event_id`
citations it relied on.

**MCP gives one tool contract, two callers.** The same `TOOL_SPECS` power both
the standalone MCP server (`python -m triage.mcp.server`, usable by Claude
Desktop or any MCP client) and the in-process agent path. Tests run the agent
fully offline via the LLM stub + a fake tool dispatch.

## Cost control

- **Prompt caching.** The system prompt + tool definitions are large and stable;
  the LLM wrapper marks them `cache_control: ephemeral` so a steady incident
  stream turns most of each request into cache reads (the `usage` cache
  read/write tokens are logged on every triage).
- **Throttling.** The router suppresses duplicate alerts per fingerprint for
  `alert_throttle_s`, so a recurring incident pages once, not continuously.
- **Severity-gated paging.** Only critical incidents page (PagerDuty); errors go
  to Slack; low-confidence warnings only hit the log sink.

## Scale-out

The service is stateless (all state in Elasticsearch), so it runs behind an HPA
(`deploy/k8s/deployment.yaml`, 2–10 replicas on CPU). Ingestion is idempotent —
`event_id` (host+ts+raw hash) is the ES `_id`, so retried or replayed batches
don't double-count. Incidents upsert by fingerprint, so concurrent replicas
converge on the same incident document.
