# GitHub Copilot Instructions for Hermes

## Project Overview

Hermes is an automated alert remediation orchestrator that processes Prometheus alerts from multiple Alertmanagers and triggers Rundeck jobs for automated remediation. It monitors job completion, verifies alert resolution, and escalates to NOC when remediation fails. Named after the Greek god known for speed and as the messenger of the gods.

## Technology Stack

- **Language**: Python 3.11+
- **Framework**: FastAPI with Uvicorn
- **Package Management**: Modern Python packaging with `pyproject.toml`, uses `uv` for fast installs
- **Testing**: pytest with pytest-asyncio
- **CI/CD**: CircleCI
- **Async**: Full async/await patterns with httpx for HTTP clients
- **Deployment**: Docker containers to Kubernetes
- **Key Dependencies**:
  - FastAPI, Uvicorn (web server)
  - httpx (async HTTP client)
  - Pydantic v2 (config validation)
  - boto3 (DynamoDB state store)
  - prometheus-client (metrics)

## Architecture & Core Concepts

### Multi-Tenant Global Service
- Receives alerts from **multiple regional Alertmanagers** (eu-west-1, us-west-2, ap-south-1)
- Automatically extracts source Alertmanager URL from `client_url` in alert payload
- Queries the **correct source Alertmanager** when verifying alert resolution

### Remediation Workflow Lifecycle
1. **Alert Received** → Extract source Alertmanager URL, trigger Rundeck job
2. **Poll Job Status** → Wait for Rundeck job completion (configurable timeout)
3. **Job Succeeded** → Wait X minutes, check if alert resolved (queries **source** Alertmanager)
4. **Alert Resolved** → Add success comment to JIRA
5. **Alert Still Firing** → Add failure comment to JIRA, escalate to Slack NOC

### State Persistence
- Use `InMemoryStateStore` for dev/testing (state lost on restart)
- Use `DynamoDBStateStore` for production (persistent across restarts)
- Workflow recovery: Resumes in-progress workflows after pod restart by reloading from DynamoDB

### Reliability Patterns
- **Alert Deduplication**: Prevents duplicate remediation for same alert (fingerprinting by alert_name + labels)
- **Cooldown Period**: Configurable minimum time between remediation attempts per alert
- **Circuit Breakers**: Automatic protection against cascading failures to external services (Rundeck, Alertmanager, JIRA, Slack)
- **Rate Limiting**: Token bucket algorithm with per-source and global limits to prevent alert storms
- **Max Concurrent Workflows**: Configurable limit to prevent resource exhaustion
- **Readiness Probes**: Health checks validate external dependency status and circuit breaker states

## Project Structure

```
src/hermes/
  ├── api/app.py                   # FastAPI app, routes, middleware, Prometheus metrics
  ├── config.py                    # Pydantic config models with environment variable substitution
  ├── clients/                     # External service clients (all async with httpx)
  │   ├── rundeck.py              # Session-based OR token auth (session preferred - no expiry)
  │   ├── alertmanager.py         # Alert queries (MUST query source Alertmanager URL)
  │   ├── jira.py                 # Comment on tickets with remediation results
  │   └── slack.py                # NOC escalation via webhook or bot token
  ├── core/                        # Core business logic
  │   ├── remediation_manager.py  # Main orchestration: workflow lifecycle, circuit breakers
  │   ├── state_store.py          # Workflow persistence (InMemory or DynamoDB)
  │   └── job_monitor.py          # Rundeck job polling with timeout
  └── utils/
      ├── audit_logger.py         # Structured JSON audit trail (separate from logs)
      └── rate_limiter.py         # Token bucket rate limiting

tests/
  ├── conftest.py                  # Pytest fixtures (mock clients, sample workflows)
  ├── test_*.py                    # Async tests using pytest-asyncio
```

## Development Practices

### Code Conventions

1. **Async Everything**: All I/O operations use `async/await`. Clients use `httpx.AsyncClient`.
2. **Pydantic Config Models**: All configuration is Pydantic v2 models with field validation.
3. **Environment Variable Substitution**: Config YAML supports `${VAR_NAME}` syntax (see `config.py`).
4. **Session-Based Rundeck Auth**: Prefer username/password (no 30-day expiry) over API tokens.
5. **Source Alertmanager Tracking**: ALWAYS extract `client_url` from alerts and query that specific Alertmanager.

### Configuration Management

- **Config File**: `config/config.yaml` (path via `CONFIG_PATH` env var)
- **Required Fields**: Each alert configuration specifies `job_id`, `required_fields`, `fields_location`
- **Fields Location**: Alerts can have fields in `commonLabels` or nested `details` - check `fields_location`
- **State Store**: Set `state_store.type` to `"memory"` (dev) or `"dynamodb"` (prod)

### Testing Guidelines

1. **Fixtures**: Use `conftest.py` fixtures (`mock_config`, `mock_rundeck_client`, `in_memory_state_store`)
2. **Async Tests**: Mark tests with `@pytest.mark.asyncio` or rely on `asyncio_mode = "auto"` in `pyproject.toml`
3. **Mocking**: Mock external clients (Rundeck, Alertmanager, JIRA, Slack) - never make real API calls
4. **Test Client**: Use `TestClient` from FastAPI for API endpoint tests
5. **Health Checks**: Test both liveness (`/health/live`) and readiness (`/health/ready`) probes

### Running Locally

```bash
# Install dependencies (uses uv for speed)
pip install -e ".[dev]"

# Run server
python -m hermes.api.app
# OR
hermes  # if installed via pip

# Run tests
pytest -v

# Run tests with coverage
pytest --cov=hermes --cov-report=term

# Format code
black src/ tests/
isort src/ tests/

# Type checking
mypy src/
```

### Docker Build

```bash
# Dockerfile uses uv for fast dependency install
docker build -t hermes:latest .

# Run container
docker run -p 8080:8080 \
  -e CONFIG_PATH=/app/config/config.yaml \
  -e RUNDECK_USERNAME=admin \
  -e RUNDECK_PASSWORD=secret \
  hermes:latest
```

## Key APIs & Endpoints

- `POST /api/v1/alerts` - Alertmanager webhook receiver (extracts `client_url`)
- `GET /api/v1/remediations` - List active workflows
- `GET /api/v1/remediations/{id}` - Get workflow details
- `GET /api/v1/rate-limits` - Rate limiter statistics
- `GET /health` - Basic health
- `GET /health/ready` - Readiness probe (checks circuit breaker states)
- `GET /health/live` - Liveness probe
- `GET /metrics` - Prometheus metrics

## Common Patterns

### Adding a New Alert Configuration

1. Add alert to `config/config.yaml`:
   ```yaml
   alerts:
     "NewAlert":
       job_id: "rundeck-job-uuid"
       required_fields: ["instance", "cluster"]
       fields_location: "commonLabels"  # or "details"
       remediation:
         enabled: true
         resolution_wait_minutes: 5
         jira_ticket_option: "jira_ticket"
   ```

2. Rundeck job must accept all `required_fields` as job options.

### Adding a New Client

1. Create in `src/hermes/clients/<service>.py`
2. Inherit patterns from existing clients (async, httpx, error handling)
3. Add circuit breaker tracking in `RemediationManager._circuit_breakers`
4. Mock in `conftest.py` for testing

### Prometheus Metrics

All metrics use `prometheus_client`:
- Counters: `ALERTS_RECEIVED_BY_TYPE`, `REMEDIATION_WORKFLOWS`, `PROCESSING_ERRORS`, `CIRCUIT_BREAKER_TRIPS`
- Histograms: `PROCESSING_DURATION`
- Labels: Use `alert_type`, `outcome`, `service`, `source` for dimensionality

## Important Notes

1. **Source Alertmanager is Critical**: The `client_url` field determines which Alertmanager to query for alert resolution. Do NOT hardcode a single Alertmanager URL.
2. **Workflow Recovery**: On pod restart, DynamoDB state store reloads active workflows and resumes background tasks.
3. **Circuit Breakers**: Services failing repeatedly will have circuit opened - check `/health/ready` for status.
4. **Rate Limiting**: Per-source limits prevent single Alertmanager from overwhelming Hermes.
5. **Cooldown**: Same alert won't trigger remediation again within cooldown period.
6. **Health Filtering**: Uvicorn access logs for `/health` are filtered out to reduce log noise.

## CI/CD Pipeline

- CircleCI runs on all branches
- **Non-main branches**: Runs tests only
- **Main branch**: Runs tests, builds Docker image, pushes to ECR with `latest` and `$CIRCLE_SHA1` tags
- Uses `uv` for fast dependency installation in CI

## Related Projects

- **sre-automations**: Contains Rundeck automation scripts that Hermes triggers
- **infrastructure**: Kubernetes manifests and ArgoCD applications for deployment
- **helm-charts**: Helm chart for Hermes deployment (likely named `alterdeck`)
