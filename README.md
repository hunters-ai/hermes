# AlterDeck

AlterDeck is a tiny HTTP service that processes Prometheus alerts and forwards them to Rundeck jobs for automated remediation / job triggering. It acts as an middleware that extracts required parameters i.e options from alert payloads and triggers corresponding Rundeck automation jobs.

## What it does :
- Receives Alertmanager webhook alerts via HTTP API
- Extracts required fields from alert payloads based on supplied config file mapping
- Automatically triggers Rundeck jobs with extracted alert data
- Exposes prometheus metrics for monitoring and stats

## To test the service : 

1. Configure a route in alertmanager to send alerts to the webhook
```yaml
receivers:
- name: alterdeck
    webhook_configs:
    - url: 'http://your-alertdeck-address:8080/api/v1/alerts'
        send_resolved: false

routes:
- match_re:
    service: your-service
    severity: error
receiver: alterdeck
```

Create a `config/config.yaml` file with the following structure:

```yaml
# Rundeck configuration
auth_token: "your-rundeck-auth-token"
base_url: "https://your-rundeck-server.com"

# Alert configurations
alerts:
  KubeNodeOutofSpace: #alertname field from alert payload
    required_fields:
      - "instance"
      - "device"
      - "mountpoint"
    job_id: "disk-cleanup-job-id"
    fields_location: "commonLabels"  # or "root" for top-level fields
```

**Example Alert Payload from alertmanager, I shared it here to understand the JSON structure:**
```json
{
  "receiver": "webhook",
  "status": "firing",
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "KubeNodeOutofSpace",
        "severity": "warning"
      },
      "startsAt": "2023-10-21T10:00:00Z",
      "generatorURL": "http://prometheus:9090/graph?g0.expr=...",
      "fingerprint": "abc123"
    }
  ],
  "commonLabels": {
    "alertname": "KubeNodeOutofSpace",
    "instance": "server01:9100",
    "device": "/dev/sda1",
    "mountpoint": "/",
    "severity": "warning"
  },
  "externalURL": "http://alertmanager:9093",
  "version": "4",
  "groupKey": "{}:{alertname=\"KubeNodeOutofSpace\"}"
}
```

### Metrics

AlterDeck exposes the following Prometheus metrics:

- `alterdeck_incoming_requests_total`: Total number of incoming alert requests
- `alterdeck_webhook_requests_total`: Total number of webhook requests sent to Rundeck
- `alterdeck_webhook_errors_total`: Total number of webhook request errors
- `alterdeck_processing_duration_seconds`: Time taken to process alert requests
- `alterdeck_alerts_received_total`: Total alerts received by type
- `alterdeck_processing_errors_total`: Total processing errors by type
- `alterdeck_rundeck_job_triggers_total`: Total Rundeck job triggers by status