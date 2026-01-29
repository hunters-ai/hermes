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
- **Circuit Breakers**: Automatic protection against cascading failures to external services
- **Rate Limiting**: Per-source and global rate limits to prevent alert storms
- **Workflow Recovery**: Resumes in-progress workflows after pod restart
- **Max Concurrent Workflows**: Configurable limit to prevent resource exhaustion
- **Readiness Probes**: Health checks that validate external dependency status
- **Audit Logging**: Structured JSON audit trail for all remediation events

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

1. **Alert Received** → Extract source Alertmanager URL from `client_url`, trigger Rundeck job
2. **Poll Job Status** → Wait for completion (configurable timeout)
3. **Job Succeeded** → Wait X minutes, check if alert resolved (queries source Alertmanager)
4. **Alert Resolved** → Add success comment to JIRA
5. **Alert Still Firing** → Add failure comment to JIRA, escalate to Slack

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

# Alert configurations (map alert names to Rundeck jobs)
alerts:
  "HighCPUUsage":
    job_id: "abc123-def456-rundeck-job-uuid"    # Rundeck job UUID
    fields_location: "commonLabels"              # Where to find fields: commonLabels, details, or root
    required_fields:
      - "instance"
      - "cluster"
    field_mappings:
      instance: "host_name"                      # Map alert field 'instance' to Rundeck option 'host_name'
    remediation:
      enabled: true
      alertmanager_check_delay_minutes: 5        # Override global setting
```

## Alert Configuration Options

Each alert in the `alerts` section supports the following configuration:

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `job_id` | string | **Required**. Rundeck job UUID to trigger for this alert |
| `required_fields` | list | **Required**. Alert fields needed for the Rundeck job (e.g., `["instance", "cluster"]`) |
| `fields_location` | string | Where to find fields in alert payload. Options: `commonLabels` (default), `details`, `root` |

### Optional Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `field_mappings` | dict | `{}` | Map alert field names to Rundeck job option names (e.g., `{"node": "node_name"}`) |

### Remediation Options

All fields under `remediation` block are optional:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable/disable auto-remediation for this alert |
| `alertmanager_check_delay_minutes` | int | Global setting (5) | Minutes to wait after job success before checking Alertmanager |
| `job_retrigger_cooldown_minutes` | int | Global setting (5) | Minimum minutes before retriggering same job for same alert |
| `jira_ticket_option` | string | `"jira_ticket"` | Rundeck job option name where JIRA ticket ID should be passed |
| `fetch_jira_ticket` | bool | `false` | Query JIRA for ticket ID before triggering Rundeck (for alerts created by jira-alert service) |
| `jira_summary_search_field` | string | `null` | Alert field to use in JIRA summary search (e.g., `"dataflow_id"`) |
| `skip_resolution_check` | bool | `false` | Skip checking Alertmanager after job succeeds (for alerts requiring customer action) |

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
        send_resolved: false  # Important: only send firing alerts
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
| "Rundeck job failed" | Verify Rundeck job options match `required_fields` and `field_mappings` |
| Alert deduplicated immediately | Check `job_retrigger_cooldown_minutes` - previous workflow may still be active |
| Circuit breaker open | Service (Rundeck/JIRA/Slack) is failing - check `/health/ready` endpoint |

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
3. Hermes extracts source Alertmanager URL from client_url
         ↓
4. Check deduplication (already remediating this alert?)
         ↓
5. Check cooldown (too soon to retry?)
         ↓
6. Check rate limits (too many alerts?)
         ↓
7. Trigger Rundeck job with alert fields
         ↓
8. Poll Rundeck every 30s for job completion
         ↓
9. Job succeeds → Wait X minutes (alertmanager_check_delay_minutes)
         ↓
10. Check source Alertmanager: is alert resolved?
         ↓
    ├── YES: Alert resolved
    │    ↓
    │   Add success comment to JIRA ✅
    │
    └── NO: Alert still firing
         ↓
        Add failure comment to JIRA ❌
         ↓
        Escalate to Slack NOC 🚨
```

**Key behaviors:**
- **Deduplication**: Same alert (same fingerprint) won't trigger duplicate jobs
- **Cooldown**: After remediation, alert must wait `job_retrigger_cooldown_minutes` before retriggering
- **Circuit Breaker**: If Rundeck/JIRA/Slack keeps failing, circuit opens to prevent cascading failures
- **Multi-tenant**: Each alert's source Alertmanager is tracked via `client_url`

## Project Structure

```
hermes/
├── src/hermes/
│   ├── api/
│   │   └── app.py                # FastAPI routes, metrics, middleware
│   ├── clients/                  # External service integrations
│   │   ├── rundeck.py           # Rundeck job execution
│   │   ├── alertmanager.py      # Alert resolution checks
│   │   ├── jira.py              # JIRA ticket comments
│   │   └── slack.py             # NOC escalations
│   ├── core/                     # Core business logic
│   │   ├── remediation_manager.py  # Workflow orchestration
│   │   ├── state_store.py          # DynamoDB/in-memory persistence
│   │   └── job_monitor.py          # Rundeck polling
│   ├── utils/
│   │   ├── audit_logger.py      # Structured audit trail
│   │   └── rate_limiter.py      # Token bucket rate limiting
│   └── config.py                 # Pydantic config models
├── tests/                        # pytest test suite
├── config/
│   └── config.yaml              # Configuration
├── Dockerfile
├── pyproject.toml               # Modern Python packaging
└── README.md
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Basic health check |
| `/health/ready` | GET | Readiness probe (checks dependencies) |
| `/health/live` | GET | Liveness probe |
| `/metrics` | GET | Prometheus metrics |
| `/api/v1/alerts` | POST | Receive Alertmanager webhooks |
| `/api/v1/remediations` | GET | List active remediation workflows |
| `/api/v1/remediations/{id}` | GET | Get workflow status |
| `/api/v1/rate-limits` | GET | Get rate limiter statistics |

## Prometheus Metrics

Hermes exposes metrics on `/metrics` for monitoring:

### Request Metrics
- `hermes_incoming_requests_total` - Total incoming alert requests
- `hermes_webhook_requests_total` - Webhook requests sent to Rundeck
- `hermes_webhook_errors_total` - Webhook request errors
- `hermes_processing_duration_seconds` - Processing time histogram

### Remediation Metrics
- `hermes_rundeck_job_triggers_total{status}` - Job triggers by status (success/failed)
- `hermes_remediation_workflows_total` - Remediation workflows started
- `hermes_remediation_outcomes_total{alert_type, outcome}` - Outcomes by result
  - `outcome=success` - Alert resolved after remediation
  - `outcome=job_failed` - Rundeck job failed
  - `outcome=alert_still_firing` - Alert not resolved after job succeeded

### Reliability Metrics
- `hermes_alerts_deduplicated_total{alert_type, reason}` - Alerts skipped due to deduplication
- `hermes_circuit_breaker_trips_total{service}` - Circuit breaker activations
- `hermes_rate_limited_requests_total{source, limit_type}` - Rate limited requests

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
| `RUNDECK_AUTH_TOKEN` | Rundeck API token (legacy - expires in 30 days, use session auth instead) |
| `JIRA_API_TOKEN` | JIRA API token (required if JIRA integration enabled) |
| `SLACK_WEBHOOK_URL` | Slack webhook URL (required if Slack integration enabled) |
| `PORT` | Server port (default: 8080) |
| `DEBUG` | Enable debug logging (`true`/`false`) |

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