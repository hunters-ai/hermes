# Hermes

**Hermes** - Swift messenger for automated alert remediation. Named after the Greek god known for speed and as the messenger of the gods.

Hermes is an automated alert remediation orchestrator that processes Prometheus alerts and triggers Rundeck jobs for automated remediation. It monitors job completion, verifies alert resolution, and escalates to NOC when remediation fails.

## Features

- **Global Service Mode**: Receives alerts from multiple Alertmanagers, automatically extracting the source URL from `client_url`
- **Alert Processing**: Receives Alertmanager webhook alerts and triggers Rundeck jobs
- **Session-Based Auth**: Supports Rundeck username/password authentication (no 30-day token expiry!)
- **Job Monitoring**: Polls Rundeck for job completion status
- **Alert Verification**: Checks Alertmanager to confirm alerts are resolved after remediation
- **JIRA Integration**: Adds comments to JIRA tickets with remediation results
- **Slack Escalation**: Notifies NOC on-call when remediation fails
- **Prometheus Metrics**: Exposes metrics for monitoring

### Reliability Features

- **Alert Deduplication**: Prevents duplicate remediation attempts for the same alert
- **Cooldown Period**: Configurable minimum time between remediation attempts per alert
- **Bounded Retries**: Per-alert `max_attempts` with cooldown-respecting auto-retrigger before escalating
- **Burst Suppression**: Detects "K distinct fingerprints fired within N minutes" per alert and pauses Rundeck triggering for that alert (with a one-shot Slack page) until the suppression window expires or an operator dismisses it
- **Early Resolution via Webhook**: Honors Alertmanager `send_resolved: true` events to short-circuit the resolution wait
- **Circuit Breakers**: Automatic protection against cascading failures to external services
- **Rate Limiting**: Per-source and global rate limits (token bucket) to prevent alert storms
- **Workflow Recovery**: Resumes in-progress workflows after pod restart from DynamoDB
- **Max Concurrent Workflows**: Configurable limit to prevent resource exhaustion
- **Readiness Probes**: Health checks that validate external dependency status
- **Audit Logging**: Structured JSON audit trail for all remediation events
- **Public Webhook Endpoint**: `X-API-Key`-authenticated route for external sources (e.g. Snowflake) sharing the same Alertmanager-shaped payload

## Architecture

```
┌─────────────────┐     ┌───────────────┐     ┌──────────┐
│ Alertmanager(s) │────▶│    Hermes     │────▶│ Rundeck  │
│ (eu-west-1)     │     │  (global)     │     │          │
│ (us-west-2)     │     └───────────────┘     └──────────┘
│ (ap-south-1)    │            │                   │
└─────────────────┘            │ (poll status)     │
       ▲                       ◀───────────────────┘
       │                       │
       │ (verify resolved)     ├───▶ JIRA (comment)
       └───────────────────────┤
                               └───▶ Slack (escalate)
```

## Remediation Workflow

1. **Alert Received** → Extract source Alertmanager URL from `client_url` / `externalURL`, run dedup + cooldown + rate-limit + circuit-breaker + burst-suppression checks
2. **Trigger Rundeck Job** → Resolve `job_id` (with optional `job_id_mappings`), apply `field_mappings` / `value_mappings` / `static_options`, optionally fan out via `split_field`
3. **Poll Job Status** → Wait for completion (configurable timeout) and persist state in DynamoDB
4. **Job Succeeded** → Wait up to `alertmanager_check_delay_minutes` for the alert to clear; either an inbound `resolved` webhook or a poll against the source Alertmanager can satisfy this early
5. **Alert Resolved** → Add success comment to JIRA
6. **Alert Still Firing** → Retrigger up to `max_attempts` (respecting cooldown), then add failure comment to JIRA and escalate to Slack NOC
7. **Job Failed** → Skip resolution wait, add job-failure comment to JIRA and escalate to Slack NOC

## Quick Start

1. **Configure** `config/config.yaml`:

```yaml
# Rundeck connection (session-based auth recommended - no expiration!)
rundeck:
  base_url: "https://rundeck.example.com"
  username: "${RUNDECK_USERNAME}"
  password: "${RUNDECK_PASSWORD}"

# Global remediation settings
remediation:
  poll_interval_seconds: 30                     # How often to check Rundeck job status
  alertmanager_check_delay_minutes: 5           # Wait time after job success before checking if alert resolved
  max_job_wait_minutes: 30                      # Rundeck job timeout
  job_retrigger_cooldown_minutes: 5             # Min time before retriggering same job for same alert
  max_concurrent_workflows: 100                 # Max parallel remediation workflows
  circuit_breaker_failure_threshold: 5          # Service failures before circuit opens
  circuit_breaker_recovery_seconds: 60          # Wait before retrying failed service
  # Rate limiting (prevent alert storms)
  rate_limit_enabled: true
  rate_limit_per_source_rate: 10.0              # Max requests/sec per Alertmanager
  rate_limit_per_source_burst: 50
  rate_limit_global_rate: 100.0                 # Max total requests/sec
  rate_limit_global_burst: 500

# Workflow persistence
state_store:
  type: "dynamodb"                              # 'memory' for dev, 'dynamodb' for production
  dynamodb_table: "hermes-workflows"
  dynamodb_region: "us-west-2"
  ttl_hours: 24                                 # How long to keep completed workflows

# JIRA integration (for commenting on tickets)
jira:
  base_url: "https://company.atlassian.net"
  api_token: "${JIRA_API_TOKEN}"
  user_email: "automation@company.com"

# Slack integration (for NOC escalation)
slack:
  webhook_url: "${SLACK_WEBHOOK_URL}"
  noc_channel: "#noc-alerts"
  noc_user_group: "noc-on-call"

# API authentication (for /api/v1/webhooks/public)
api:
  webhook_api_key: "${HERMES_WEBHOOK_API_KEY}"   # Required header: X-API-Key

# Alert configurations (map alert names to Rundeck jobs)
alerts:
  "HighCPUUsage":
    job_id: "abc123-def456-rundeck-job-uuid"    # Rundeck job UUID (default; can be overridden by job_id_mappings)
    fields_location: "commonLabels"              # Where to find fields: commonLabels, details, or root
    required_fields:
      - "instance"
      - "cluster"
    field_mappings:
      instance: "host_name"                      # Map alert field 'instance' to Rundeck option 'host_name'
    remediation:
      enabled: true
      max_attempts: 2                            # Override global retry policy
      alertmanager_check_delay_minutes: 5        # Override global setting
      static_options:                            # Always-passed Rundeck options
        component: "node"
      burst_suppression:                         # Pause triggering on infra-wide spikes
        enabled: false
        threshold: 5
        window_minutes: 10
        suppression_minutes: 30
```

## Alert Configuration Options

Each alert in the `alerts` section supports the following configuration:

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `job_id` | string | **Required**. Default Rundeck job UUID to trigger for this alert (can be overridden per-fire by `job_id_mappings`) |
| `required_fields` | list | **Required**. Alert fields needed for the Rundeck job (e.g., `["instance", "cluster"]`) |
| `fields_location` | string | Where to find fields in alert payload. Options: `commonLabels` (default), `details`, `root` |

### Optional Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `field_mappings` | dict | `{}` | Map alert field names to Rundeck job option names (e.g., `{"node": "node_name"}`) |
| `value_mappings` | dict | `{}` | Conditionally set Rundeck options based on alert label values (e.g., `{"region": {"us-west-2": {"cluster_name": "us-west-2-prod-new"}}}`) |
| `job_id_mappings` | dict | `{}` | Conditionally route to a different Rundeck `job_id` based on a field value (e.g., `{"error_message": {"vendor-down": "<uuid>"}}`); falls back to `job_id` if no value matches |
| `split_field` | string | `null` | Field whose value is split into multiple workflows (one Rundeck trigger per split value); deduped via a synthetic `__split_<field>` label |
| `split_delimiter` | string | `","` | Delimiter used to split `split_field` (e.g., `", "`) |

### Remediation Options

All fields under `remediation` block are optional:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable/disable auto-remediation for this alert |
| `alertmanager_check_delay_minutes` | int | Global setting (5) | Minutes to wait after job success before checking Alertmanager |
| `job_retrigger_cooldown_minutes` | int | Global setting (5) | Minimum minutes before retriggering same job for same alert |
| `max_attempts` | int | Global setting (2) | Override max retry attempts (initial + retries) for this alert |
| `jira_ticket_option` | string | `"jira_ticket"` | Rundeck job option name where JIRA ticket ID should be passed |
| `fetch_jira_ticket` | bool | `false` | Query JIRA for ticket ID before triggering Rundeck (for alerts created by jira-alert service) |
| `jira_summary_search_field` | string | `null` | Alert field to use in JIRA summary search (e.g., `"dataflow_id"`) |
| `skip_resolution_check` | bool | `false` | Skip checking Alertmanager after job succeeds (for alerts requiring customer action) |
| `send_alert_payload` | bool | `false` | Send full alert context as JSON string to Rundeck job for debugging/JIRA comments |
| `alert_payload_option_name` | string | `"alert_payload"` | Rundeck job option name to receive the alert payload JSON string |
| `static_options` | dict | `{}` | Static key/value pairs always passed to the Rundeck job options (also fall back as values for `required_fields` not present in the alert) |
| `burst_suppression` | object | `{enabled: false}` | Per-alert burst suppression config (see below) |

### Burst Suppression Options

All fields under `remediation.burst_suppression` are optional:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable burst suppression for this alert |
| `threshold` | int | `5` | K: trip when this many distinct fingerprints fire within the window |
| `window_minutes` | int | `10` | N: rolling window length in minutes |
| `suppression_minutes` | int | `30` | How long suppression stays active after a trip (also dismissable via the admin endpoint) |

> Counters and the suppression flag are **in-memory, per-pod**, lost on restart. The deployment is single-pod today; if it scales out, the effective threshold becomes `threshold * pod_count` because each pod counts independently.

### Example Configurations

**Simple alert with fast retry:**
```yaml
'Node NotReady':
  job_id: "4d47bff5-c549-48da-9aad-d5ac0619fc94"
  fields_location: "commonLabels"
  required_fields:
    - "node"
    - "cluster"
  field_mappings:
    node: "node_name"  # Alert has 'node', Rundeck expects 'node_name'
  remediation:
    enabled: true
    job_retrigger_cooldown_minutes: 2  # Retry faster for critical infra
```

**Alert with full payload forwarding (for debugging/JIRA comments):**
```yaml
'Node NotReady':
  job_id: "4d47bff5-c549-48da-9aad-d5ac0619fc94"
  fields_location: "commonLabels"
  required_fields:
    - "node"
    - "cluster"
    - "region"
  remediation:
    enabled: true
    send_alert_payload: true              # Send full alert context to Rundeck
    alert_payload_option_name: "alert_payload"  # Rundeck option name
```

The alert payload JSON includes:
- `alert_name`: Alert name
- `alert_labels`: All alert labels
- `alert_time`: When alert fired
- `source_alertmanager`: Source Alertmanager URL
- `processed_options`: Extracted required fields

Example payload structure:
```json
{
  "alert_name": "Node is in NotReady state for 30 minutes",
  "alert_labels": {
    "alertname": "Node is in NotReady state for 30 minutes",
    "cluster": "eu-west-1-prod",
    "node": "ip-172-26-75-139.eu-west-1.compute.internal",
    "region": "eu-west-1",
    "severity": "warning"
  },
  "alert_time": "2026-01-29T11:46:48Z",
  "source_alertmanager": "https://alertmanager.eu-west-1.hunters.ai",
  "processed_options": {
    "node": "ip-172-26-75-139",
    "cluster": "eu-west-1-prod"
  }
}
```

In your Rundeck job script, parse the JSON:
```python
import json
alert_payload = json.loads("${option.alert_payload}")
print(f"Remediating alert: {alert_payload['alert_name']}")
```

**Alert with JIRA ticket lookup:**
```yaml
'Dataflow Internal Error':
  job_id: "6f037e9c-8342-49d3-b035-105b6e9abf77"
  fields_location: "commonLabels"
  required_fields:
    - "dataflow_id"
    - "cluster"
  remediation:
    enabled: true
    fetch_jira_ticket: true              # Query JIRA for ticket
    jira_summary_search_field: "dataflow_id"  # Search JIRA using this field
    skip_resolution_check: true          # Don't wait for alert to resolve
    job_retrigger_cooldown_minutes: 30   # Long cooldown - requires customer action
```

**Alert with conditional Rundeck job routing:**
```yaml
'FN Dataflow Internal Issues':
  fields_location: "commonLabels"
  job_id: "6f037e9c-8342-49d3-b035-105b6e9abf77"  # Default fallback
  job_id_mappings:                                # Route to different jobs by error_message
    error_message:
      "Data-collection-vendor-is-down": "07262d42-6883-40be-8af2-69a19ed0744d"
      "Active-dataflow-is-not-running-or-failed": "c215a485-b533-4441-a722-1073be2a7cb8"
  required_fields:
    - "dataflow_id"
    - "cluster"
    - "error_message"
  field_mappings:
    cluster: "cluster_name"
  remediation:
    enabled: true
    skip_resolution_check: true
```

**Alert with splitting (one webhook → multiple workflows):**
```yaml
'Snowflake Queue High':
  fields_location: "commonLabels"
  job_id: "9d4bf3d9-9c58-483e-9ced-44682ab4bf6f"
  split_field: "warehouse_names"        # "WH1, WH2, WH3" -> 3 separate workflows
  split_delimiter: ", "
  required_fields:
    - "warehouse_names"
    - "account_identifier"
    - "region"
  field_mappings:
    warehouse_names: "warehouse"        # Each split value mapped to the 'warehouse' option
  remediation:
    enabled: true
    skip_resolution_check: true
    job_retrigger_cooldown_minutes: 30
```

**Alert with burst suppression (pause on infra-wide spikes):**
```yaml
'Snowpipe infrastructure issues detected':
  fields_location: "commonLabels"
  job_id: "00d3661d-a008-49aa-9388-60acfbd5612a"
  required_fields:
    - "dataflow_id"
    - "puller_id"
  remediation:
    enabled: true
    max_attempts: 2
    job_retrigger_cooldown_minutes: 60
    alertmanager_check_delay_minutes: 60
    burst_suppression:
      enabled: true
      threshold: 5            # 5 distinct fingerprints
      window_minutes: 10      # in 10 min
      suppression_minutes: 30 # pause Rundeck triggering for 30 min, then auto-lift
```

## How to Add a New Alert

### Step 1: Create/Identify Rundeck Job

1. Go to your Rundeck instance
2. Create a new job or find existing job UUID
3. Note the job option names (e.g., `instance`, `cluster`, `jira_ticket`)

### Step 2: Add Alert to Hermes Config

Add to `config/config.yaml` under `alerts`:

```yaml
alerts:
  'Your Alert Name':  # Must match alertname from Prometheus
    job_id: "your-rundeck-job-uuid-here"
    fields_location: "commonLabels"  # Where alert fields are located
    required_fields:
      - "field1"  # Fields needed by Rundeck job
      - "field2"
    field_mappings:  # Optional: map alert fields to Rundeck options
      field1: "rundeck_option_name"
    remediation:
      enabled: true
      # Optional overrides:
      # alertmanager_check_delay_minutes: 5
      # job_retrigger_cooldown_minutes: 5
```

### Step 3: Configure Alertmanager

Add Hermes as a receiver in Alertmanager config:

```yaml
# alertmanager.yml
receivers:
  - name: 'hermes'
    webhook_configs:
      - url: 'http://hermes:8080/api/v1/alerts'
        send_resolved: true   # Recommended: lets Hermes short-circuit the resolution wait early
        http_config:
          follow_redirects: true

route:
  receiver: 'default-receiver'
  routes:
    # Route specific alerts to Hermes
    - match:
        alertname: 'Your Alert Name'
      receiver: 'hermes'
      continue: true  # Also send to other receivers (PagerDuty, etc.)
    
    # Or route by label
    - match:
        remediation: 'automated'
      receiver: 'hermes'
      continue: true
```

### Step 4: Test the Alert

Use the test script to send a sample alert:

```bash
# From helm-charts directory
./test-hermes-alert.sh hermes http://hermes.example.com/api/v1/alerts
```

Or manually trigger via Alertmanager:

```bash
curl -XPOST http://alertmanager.example.com/api/v2/alerts \
  -H "Content-Type: application/json" \
  -d '[{
    "labels": {
      "alertname": "Your Alert Name",
      "field1": "value1",
      "field2": "value2"
    }
  }]'
```

### Step 5: Monitor and Verify

Check Hermes logs:
```bash
kubectl logs -n your-namespace deploy/hermes -f
```

Check metrics:
```bash
curl http://hermes:8080/metrics | grep hermes_remediation
```

Verify workflow:
```bash
curl http://hermes:8080/api/v1/remediations
```

### Troubleshooting New Alerts

| Issue | Solution |
|-------|----------|
| "no configuration found for alert" | Alert name in config must exactly match `alertname` label |
| "Missing required fields" | Check `fields_location` - try `commonLabels`, `details`, or `root` |
| "Rundeck job failed" | Verify Rundeck job options match `required_fields`, `field_mappings`, and any `static_options` |
| Alert deduplicated immediately | Check `job_retrigger_cooldown_minutes` - previous workflow may still be active |
| Circuit breaker open | Service (Rundeck/JIRA/Slack) is failing - check `/health/ready` endpoint |
| Alerts being silently dropped | Check `GET /api/v1/burst-suppressions` — the alert may be in burst suppression. Dismiss via `POST /api/v1/admin/burst-suppression/<alert_name>/dismiss` (`X-API-Key` required) |
| Public webhook returns 401/403 | Confirm `HERMES_WEBHOOK_API_KEY` is set and the caller sends matching `X-API-Key` |

## Running Locally

```bash
# Install dependencies
pip install -e ".[dev]"

# Set required environment variables
export CONFIG_PATH="config/config.yaml"
export RUNDECK_USERNAME="your-username"
export RUNDECK_PASSWORD="your-password"
export JIRA_API_TOKEN="your-jira-token"
export SLACK_WEBHOOK_URL="your-slack-webhook"

# Run the server
python -m hermes.api.app
# OR
hermes  # if installed via pip

# Server will start on http://localhost:8080
```

**Deploy to Kubernetes:**
```bash
helm install hermes helm-charts/hermes --values values-ops.yaml
```

## Understanding the Remediation Workflow

Here's what happens when an alert fires:

```
1. Alert fires in Prometheus
         ↓
2. Alertmanager sends webhook to Hermes
         ↓
3. Hermes extracts source Alertmanager URL from client_url / externalURL
         ↓
4. Check rate limits (per-source / global token bucket)
         ↓
5. Check deduplication (already remediating this alert?) + cooldown
         ↓
6. Check circuit breaker (Rundeck reachable?)
         ↓
7. Check burst suppression (infra-wide spike for this alert_type?)
         ↓
8. Resolve job_id (job_id_mappings) and apply field_mappings / value_mappings / static_options
         ↓
9. (Optional) Split on split_field → one workflow per split value
         ↓
10. Trigger Rundeck job; persist workflow in DynamoDB
         ↓
11. Poll Rundeck every poll_interval_seconds for job completion
         ↓
12. Job succeeds → Wait up to alertmanager_check_delay_minutes
       (resolved-event webhook can short-circuit this wait)
         ↓
13. Check source Alertmanager: is alert resolved?
         ↓
    ├── YES: Alert resolved
    │    ↓
    │   Add success comment to JIRA ✅
    │
    └── NO: Alert still firing
         ↓
        If attempts < max_attempts → wait remaining cooldown, retrigger Rundeck (loop)
         ↓
        Else add failure comment to JIRA ❌
         ↓
        Escalate to Slack NOC 🚨
```

**Key behaviors:**
- **Deduplication**: Same alert (same fingerprint = `alert_name + sorted(labels)`) won't trigger duplicate jobs
- **Cooldown**: After remediation, alert must wait `job_retrigger_cooldown_minutes` before retriggering
- **Bounded retries**: `max_attempts` caps automatic retriggers; subsequent failures escalate
- **Burst suppression**: When K distinct fingerprints fire within N minutes for an alert with `burst_suppression.enabled`, Hermes pages NOC once and drops further fires for that alert until auto-lift or admin dismiss
- **Early resolution**: An inbound `status=resolved` webhook from Alertmanager (or a poll hit) ends the resolution wait immediately
- **Circuit Breaker**: If Rundeck/JIRA/Slack keep failing, circuit opens to prevent cascading failures
- **Workflow recovery**: On pod restart, in-flight workflows from DynamoDB are resumed from their last persisted state (anything older than 24h is escalated as stale)
- **Multi-tenant**: Each alert's source Alertmanager is tracked per workflow via `client_url` / `externalURL`

## Project Structure

```
hermes/
├── src/hermes/
│   ├── api/
│   │   └── app.py                # FastAPI routes, middleware, lifespan, AlertProcessor
│   ├── clients/                  # External service integrations (all async, httpx)
│   │   ├── rundeck.py            # Token + session-cookie auth, auto re-login on 401/403
│   │   ├── alertmanager.py       # Alert resolution checks (per-tenant base URL)
│   │   ├── jira.py               # ADF v3 comments + JQL ticket lookup
│   │   └── slack.py              # NOC escalations + burst-suppression notifications
│   ├── core/                     # Core business logic
│   │   ├── remediation_manager.py  # Workflow orchestration, retries, circuit breakers, burst suppression
│   │   ├── state_store.py          # InMemory + DynamoDB persistence
│   │   └── job_monitor.py          # Rundeck execution polling helper
│   ├── utils/
│   │   ├── metrics.py           # Single source of truth for Prometheus metrics + helpers
│   │   ├── audit_logger.py      # Structured JSON audit trail
│   │   └── rate_limiter.py      # Token bucket rate limiting
│   └── config.py                 # Pydantic v2 config models + ${ENV_VAR} substitution
├── tests/                        # pytest + pytest-asyncio test suite
│   ├── conftest.py
│   ├── test_deduplication.py
│   ├── test_circuit_breaker.py
│   ├── test_rate_limiter.py
│   ├── test_burst_suppression.py
│   ├── test_workflow_recovery.py
│   ├── test_metrics.py
│   ├── test_audit_logger.py
│   ├── test_health_endpoints.py
│   ├── test_public_webhook.py
│   ├── test_alert_payload_forwarding.py
│   ├── test_value_mappings.py
│   └── test_job_id_mappings.py
├── config/
│   ├── config.yaml              # Live runtime config (committed; secrets via ${ENV_VAR})
│   └── config.example.yaml      # Reference example
├── dashboards/                  # Grafana dashboards + Prometheus recording/alerting rules
├── .circleci/config.yml         # Tests on every branch; build+push to ECR on `main`
├── Dockerfile                   # python:3.11-slim + uv; runs as non-root appuser
├── pyproject.toml               # Modern Python packaging + tool configs (black/isort/ruff/mypy/pytest)
└── README.md
```

## API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/health` | GET | – | Basic health check |
| `/health/ready` | GET | – | Readiness probe (DynamoDB + circuit breakers) — returns 503 when unhealthy |
| `/health/live` | GET | – | Liveness probe |
| `/metrics` | GET | – | Prometheus metrics |
| `/api/v1/alerts` | POST | – | Internal Alertmanager webhook receiver (handles both `firing` and `resolved` payloads) |
| `/api/v1/webhooks/public` | POST | `X-API-Key` | Public webhook for external sources (Snowflake etc.) sharing the Alertmanager payload shape |
| `/api/v1/remediations` | GET | – | List active remediation workflows |
| `/api/v1/remediations/{id}` | GET | – | Get workflow status |
| `/api/v1/rate-limits` | GET | – | Get rate limiter statistics (per-source + global) |
| `/api/v1/burst-suppressions` | GET | – | List currently-suppressed alert types and their window counters (per-pod, in-memory) |
| `/api/v1/admin/burst-suppression/{alert_name}/dismiss` | POST | `X-API-Key` | Operator dismiss of an active burst suppression; 404 if not active |

## Prometheus Metrics

Hermes exposes metrics on `/metrics` for monitoring. Metrics are defined in
`src/hermes/utils/metrics.py` (the single source of truth) and grouped below by
the question they help answer.

### HTTP layer
- `hermes_incoming_requests_total{endpoint,status}` - Incoming HTTP requests by route and status code
- `hermes_processing_duration_seconds{endpoint}` - Per-route latency histogram

### Alert intake / dedup
- `hermes_alerts_received_total{alert_type}` - Alerts received per type
- `hermes_processing_errors_total{error_type,alert_type}` - Errors raised while handling inbound alerts
- `hermes_alerts_deduplicated_total{alert_type,reason}` - Alerts skipped (`reason` ∈ `active_workflow|cooldown|concurrency_limit|other`)
- `hermes_concurrency_limit_hit_total{alert_type}` - Workflows rejected because the global cap was reached
- `hermes_rate_limited_requests_total{source,limit_type}` - Rate limited requests
- `hermes_burst_suppressions_total{alert_type,phase}` - Burst-suppression events; `phase` ∈ `tripped|dropped|dismissed`
- `hermes_burst_suppression_active{alert_type}` - Whether burst suppression is currently active for an alert type (0/1)

### Workflow lifecycle
- `hermes_remediation_workflows_total{alert_type}` - Workflows started
- `hermes_remediation_outcomes_total{alert_type,outcome,attempts}` - Terminal outcomes; `attempts` lets you compute first-attempt success rate
  - `outcome ∈ success | job_success_no_resolution_check | job_failed | alert_still_firing | retrigger_failed | missing_options | escalated`
- `hermes_remediation_retries_total{alert_type,attempt_number}` - Retry attempts initiated (`attempt_number=2` means first retry)
- `hermes_resolution_source_total{alert_type,source}` - How resolution was determined (`webhook|polling|skip_resolution_check|timeout`)
- `hermes_workflow_recovery_total{outcome}` - Outcomes of workflow recovery on restart
- `hermes_active_workflows` - In-flight workflow gauge

### Durations
- `hermes_remediation_duration_seconds{alert_type,outcome}` - End-to-end workflow duration
- `hermes_job_execution_duration_seconds{alert_type,status}` - Rundeck job runtime
- `hermes_alert_resolution_wait_seconds{alert_type,method}` - Time spent waiting for the alert to clear after job success

### External services
- `hermes_external_call_duration_seconds{service,operation,status}` - External call latency
- `hermes_external_call_errors_total{service,operation,error_type}` - External call failures
- `hermes_rundeck_job_triggers_total{alert_type,status}` - Rundeck job trigger outcomes
- `hermes_jira_operations_total{operation,status}` - JIRA writes/reads issued by Hermes
- `hermes_jira_ticket_fetch_total{alert_type,status}` - Legacy JIRA ticket-lookup counter (kept for back-compat)

### Reliability
- `hermes_circuit_breaker_trips_total{service}` - Circuit breaker activations
- `hermes_circuit_breaker_state{service}` - Current breaker state (0=closed, 1=open)

### Notifications
- `hermes_escalations_sent_total{alert_type,target,result}` - Escalations dispatched
- `hermes_slack_notifications_total{type,status}` - Slack send attempts (`type` ∈ `escalation|burst_suppression`)

### Build / config
- `hermes_build_info{version,commit,config_hash}` - Always 1; use the labels for deploy/version tracking. `commit` is taken from the `HERMES_GIT_SHA` env var; `config_hash` is the SHA-256 (first 12 hex) of the loaded config file

## Environment Variables

### Required (loaded from AWS Secrets Manager in production)

| Variable | Description |
|----------|-------------|
| `CONFIG_PATH` | Path to config file (default: `config/config.yaml`) |
| `RUNDECK_USERNAME` | Rundeck username (for session-based auth) |
| `RUNDECK_PASSWORD` | Rundeck password (for session-based auth) |

### Optional

| Variable | Description |
|----------|-------------|
| `RUNDECK_AUTH_TOKEN` | Rundeck API token (legacy - expires in 30 days, use session auth instead). Wire it via `${RUNDECK_AUTH_TOKEN}` under `rundeck.auth_token` if used |
| `JIRA_API_TOKEN` | JIRA API token (required if JIRA integration enabled) |
| `SLACK_WEBHOOK_URL` | Slack webhook URL (required if Slack integration enabled) |
| `HERMES_WEBHOOK_API_KEY` | API key required by the public webhook (`X-API-Key` header). Substituted into `api.webhook_api_key` |
| `HERMES_GIT_SHA` | Build/commit SHA reported via the `hermes_build_info` metric (default: `unknown`) |
| `PORT` | Server port (default: 8080) |
| `DEBUG` | Enable verbose payload logging in `/api/v1/alerts` (`true`/`false`) |

Secrets in `config/config.yaml` are referenced as `${VAR_NAME}`. **Unset variables silently keep the literal `${VAR_NAME}` string** — make sure required env vars are present at startup.

**Production deployment:** In Kubernetes, these are loaded from AWS Secrets Manager via the pod's secret volume mount.

## Development

### Setup

```bash
# Clone and install
git clone <repo-url>
cd hermes
pip install -e ".[dev]"
```

### Running Tests

```bash
# Run all tests
pytest

# With coverage
pytest --cov=hermes --cov-report=html

# Specific test file
pytest tests/test_deduplication.py -v
```

### Code Quality

```bash
# Format
black src tests
isort src tests

# Lint
ruff check src tests

# Type check
mypy src
```

### Local Testing with Docker

```bash
# Build
docker build -t hermes:local .

# Run with env vars
docker run -p 8080:8080 \
  -e CONFIG_PATH=/app/config/config.yaml \
  -e RUNDECK_USERNAME=admin \
  -e RUNDECK_PASSWORD=secret \
  -v $(pwd)/config:/app/config \
  hermes:local
```

## Contributing

1. Create feature branch from `main`
2. Make changes with tests
3. Run `pytest` and ensure all tests pass
4. Run `black` and `isort` for formatting
5. Submit PR with clear description

## License

[Add your license here]