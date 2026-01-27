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
# Session-based auth (recommended - no expiration!)
rundeck:
  base_url: "https://rundeck.example.com"
  username: "${RUNDECK_USERNAME}"
  password: "${RUNDECK_PASSWORD}"

remediation:
  poll_interval_seconds: 30
  resolution_wait_minutes: 5
  max_job_wait_minutes: 30

# Optional: JIRA integration
jira:
  base_url: "https://company.atlassian.net"
  api_token: "${JIRA_API_TOKEN}"
  user_email: "automation@company.com"

# Optional: Slack escalation
slack:
  webhook_url: "${SLACK_WEBHOOK_URL}"
  noc_channel: "#noc-alerts"
  noc_user_group: "noc-on-call"

# Alert configurations
alerts:
  "Investigation pipeline delay is more than 20 minutes":
    job_id: "rundeck-job-uuid"
    fields_location: "details"
    required_fields:
      - "org_code"
      - "service"
    remediation:
      enabled: true
      jira_ticket_option: "jira_ticket"
```

2. **Run locally**:
```bash
pip install -r requirements.txt
python main.py
```

3. **Deploy to Kubernetes**:
```bash
helm install hermes helm-charts/alterdeck --values values.yaml
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/metrics` | GET | Prometheus metrics |
| `/api/v1/alerts` | POST | Receive Alertmanager webhooks |
| `/api/v1/remediations` | GET | List active remediation workflows |
| `/api/v1/remediations/{id}` | GET | Get workflow status |

## Alertmanager Configuration

```yaml
receivers:
  - name: hermes
    webhook_configs:
      - url: 'http://hermes:8080/api/v1/alerts'
        send_resolved: false

routes:
  - match:
      severity: critical
    receiver: hermes
```

## Metrics

| Metric | Description |
|--------|-------------|
| `hermes_incoming_requests_total` | Total incoming alert requests |
| `hermes_webhook_requests_total` | Webhook requests sent to Rundeck |
| `hermes_webhook_errors_total` | Webhook request errors |
| `hermes_processing_duration_seconds` | Processing time histogram |
| `hermes_rundeck_job_triggers_total` | Job triggers by status |
| `hermes_remediation_workflows_total` | Remediation workflows started |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `CONFIG_PATH` | Path to config file (default: `config/config.yaml`) |
| `RUNDECK_USERNAME` | Rundeck username (for session auth) |
| `RUNDECK_PASSWORD` | Rundeck password (for session auth) |
| `RUNDECK_AUTH_TOKEN` | Rundeck API token (legacy, expires in 30 days) |
| `JIRA_API_TOKEN` | JIRA API token |
| `SLACK_WEBHOOK_URL` | Slack webhook URL |
| `PORT` | Server port (default: 8080) |
| `DEBUG` | Enable debug logging |