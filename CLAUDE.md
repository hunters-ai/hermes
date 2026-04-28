# Project Context for Code Review

Hermes is a Python/FastAPI alert‑remediation orchestrator (owned by the **SRE team / `sre@hunters.ai`**) that receives Prometheus Alertmanager webhooks from multiple regional Alertmanagers, triggers Rundeck jobs to remediate, verifies resolution, and escalates to JIRA + Slack NOC when remediation fails.

## Repository Structure

```
hermes/
├── src/hermes/                      # Application package (installable, entrypoint: `hermes` -> hermes.api.app:run_server)
│   ├── __init__.py                  # Re-exports Config, RemediationManager, StateStore, etc.
│   ├── config.py                    # Pydantic v2 config models + YAML loader with ${ENV_VAR} substitution
│   ├── api/
│   │   └── app.py                   # FastAPI app: routes, Prometheus metrics, middleware, lifespan, AlertProcessor
│   ├── clients/                     # External-service clients (all async, httpx-based)
│   │   ├── rundeck.py               #   Token OR username/password (session-cookie) auth, auto re-login on 401/403
│   │   ├── alertmanager.py          #   Queries /api/v2/alerts, used to verify alert resolution
│   │   ├── jira.py                  #   ADF v3 comments + JQL search for ticket lookup
│   │   └── slack.py                 #   Webhook OR bot-token escalation with rich Block Kit message
│   ├── core/                        # Business logic
│   │   ├── remediation_manager.py   #   Workflow orchestration, circuit breakers, cooldowns, retry, recovery
│   │   ├── state_store.py           #   Abstract StateStore + InMemoryStateStore + DynamoDBStateStore
│   │   └── job_monitor.py           #   Thin Rundeck-execution polling helper on top of RundeckClient
│   └── utils/
│       ├── audit_logger.py          # Structured JSON audit trail (separate logger, no propagation)
│       └── rate_limiter.py          # Token-bucket per-source + global rate limiter
├── tests/                           # pytest + pytest-asyncio suite
│   ├── conftest.py                  # Shared fixtures: mock_config, mock_rundeck_client, in_memory_state_store, sample_workflow/payload
│   ├── test_deduplication.py
│   ├── test_circuit_breaker.py
│   ├── test_rate_limiter.py
│   ├── test_audit_logger.py
│   ├── test_workflow_recovery.py
│   ├── test_health_endpoints.py
│   ├── test_metrics.py
│   ├── test_public_webhook.py
│   ├── test_alert_payload_forwarding.py
│   ├── test_value_mappings.py
│   └── test_job_id_mappings.py
├── config/
│   ├── config.yaml                  # Live runtime config (committed; secrets via ${ENV_VAR})
│   ├── config.example.yaml          # Reference example
│   ├── config.yaml.example          # Older reference example
│   └── config.go                    # **Legacy Go stub** — unused by Python service, leftover from prior implementatio
├── .circleci/config.yml             # Tests on every branch; build+push to ECR on `main`
├── Dockerfile                       # python:3.11-slim + uv; runs as non-root `appuser`; exposes 8080
├── pyproject.toml                   # Modern packaging (setuptools), tool configs (black/isort/ruff/mypy/pytest)
├── requirements.txt                 # Runtime + test deps (used by Dockerfile/CI for fast `uv pip install`)
├── README.md                        # User-facing docs (architecture, alert config, deployment)
└── uv.lock                          # uv lockfile
```

## Key Conventions

- **Python 3.11+, async everywhere.** All I/O (Rundeck, Alertmanager, JIRA, Slack, DynamoDB) goes through `async`/`await`. HTTP clients are `httpx.AsyncClient`. DynamoDB calls (sync boto3) are wrapped via `loop.run_in_executor`. **Never** introduce blocking I/O in hot paths.
- **Pydantic v2 config.** All config goes through models in `src/hermes/config.py`. The YAML loader (`load_alert_config`) supports `${VAR_NAME}` substitution; **unset vars silently keep the literal `${VAR_NAME}` string** — handle missing secrets explicitly when adding new fields.
- **Multi-tenant / global service mode is non-negotiable.** Each alert carries the source Alertmanager via `externalURL` / `client_url`; `extract_alertmanager_url` parses it (and force-upgrades `http→https` for `hunters.ai` hosts). Workflows persist `alertmanager_url` and **must query that exact instance** for resolution checks. Do not hardcode Alertmanager URLs.
- **Two state stores, same interface.** `InMemoryStateStore` for dev/tests; `DynamoDBStateStore` (table `hunters-ops-hermes-workflows`, GSIs `execution_id-index`, `alert_name-state-index`, TTL via `ttl` attribute) for prod. New persistence fields must be added to **both** `_workflow_to_item` and `_item_to_workflow`.
- **Reliability primitives live on `RemediationManager`:** in-memory circuit breakers (`_circuit_breakers`: rundeck/alertmanager/jira/slack), in-memory cooldowns (`_alert_cooldowns`), running-task map (`_running_tasks`), early-resolution events (`_resolution_events`). They are **not persisted across restarts** — only workflow state is.
- **Alert fingerprinting** for dedup is `alert_name + sorted(labels)`. Splitting via `split_field` injects a synthetic `__split_<field>` label so each split has a distinct fingerprint.
- **Per-alert overrides win over global** for `alertmanager_check_delay_minutes`, `job_retrigger_cooldown_minutes`, `max_attempts`, `jira_ticket_option` (see `AlertRemediationConfig`).
- **Rundeck auth:** prefer `username` + `password` (session cookie auto-managed, auto-relogin on 401/403). Token auth (`X-Rundeck-Auth-Token`) is legacy with a 30-day expiry — there's a `TODO` in `rundeck.py` about deprecating session auth once a non-expiring token mechanism is found.
- **Audit logger ≠ operational logger.** `hermes.audit` writes structured JSON, `propagate = False`, intended for long-retention sinks (CloudWatch/S3). Don't add `logger.info`-style content to it.
- **Prometheus metrics live in `api/app.py`** as module-level Counters/Histograms. Use existing label dimensions (`alert_type`, `outcome`, `service`, `source`, `limit_type`) — adding new label values silently inflates cardinality.
- **Health endpoints:** `/health` is the basic probe; `/health/ready` returns 503 if DynamoDB is unreachable or any circuit breaker is open; `/health/live` is intentionally trivial. Uvicorn access logs for `/health*` are filtered by `HealthCheckFilter`.
- **Public webhook endpoint** (`POST /api/v1/webhooks/public`) requires header `X-API-Key` matching `api.webhook_api_key`. The internal endpoint (`/api/v1/alerts`) is unauthenticated — it's expected to be cluster-internal only.
- **Code style:** black + isort (line length 100, profile=black), ruff (E/W/F/I/B/C4/UP, ignores E501/B008), mypy (`ignore_missing_imports`, tests excluded). pytest is configured with `asyncio_mode = "auto"`, so most tests don't need `@pytest.mark.asyncio`.
- **Comments are sparse and intentional.** Don't narrate what the code does; preserve existing TODOs (e.g., the Rundeck token TODO, the rate-limiter TODO in `app.py`).
- **CI:** non-`main` branches run `pytest` only; `main` additionally builds and pushes a `linux/amd64` image to ECR (`hermes` repo) tagged `latest` and `$CIRCLE_SHA1`. CircleCI workdir is `~/alterdeck` (legacy name; the helm chart is also referred to as `alterdeck`).

## Review Focus Areas

- **Multi-tenant correctness.** Any code that calls `AlertmanagerClient` must use `workflow.alertmanager_url` (extracted from the inbound payload) — falling back to a single configured Alertmanager defeats the whole design. Flag any hardcoded Alertmanager URL or any path that drops `client_url` / `externalURL`.
- **Dedup / cooldown / circuit-breaker race conditions.** `check_deduplication` → `send_to_webhook` → `start_remediation` is **not atomic**; two concurrent identical alerts can both pass the check before either persists. Watch for new code that widens this window. Cooldowns and circuit breakers are in-process only — don't assume they're shared across replicas.
- **Workflow recovery on restart.** `recover_active_workflows` skips workflows >24h old (escalates them) and resumes the rest by jumping back into `_monitor_workflow`. New states added to `RemediationState` need handling in both `recover_active_workflows` (resume vs skip) and `list_active` (active vs terminal). The DynamoDB `list_active` issues one `scan` per active state — flag any addition to `RemediationState` that would multiply this cost, or any code that introduces an unbounded scan.
- **Retry / escalation semantics.** `_handle_alert_still_firing` retriggers using the stored `rundeck_options`; if those are missing, it escalates instead. `attempts` is incremented only on actual retrigger. Make sure new retry paths preserve this and respect `max_attempts` (per-alert override or global default of 2).
- **Async correctness.** No `time.sleep`, no `requests`, no synchronous DB calls on the event loop. New external calls must be wrapped in their own `httpx.AsyncClient` context (do **not** introduce a shared client without lifecycle management) or `run_in_executor` for boto3.
- **Secret hygiene.** Never log Rundeck/JIRA/Slack credentials, JSESSIONID values beyond the existing `[:8]` prefix, or full alert payloads outside the `DEBUG=true` branch in `app.py`. The Pydantic loader does **not** raise on unset `${VAR}` — review code that builds clients from such fields to make sure it fails closed.
- **Rundeck client edge cases.** `_parse_argstring` is naive (whitespace split, no quoting) — fine for the current Rundeck format but flag any reliance on it for option values containing spaces. The session cookie is per-`RundeckClient` instance and re-acquired on 401/403; concurrent first-time logins can race (acceptable today, flag if used at higher concurrency).
- **`/api/v1/alerts` vs `/api/v1/webhooks/public`.** The two endpoints contain large blocks of duplicated logic (rate limiting, dedup, split processing, workflow start, audit logging). Bug fixes must be applied to both, or refactored into a shared helper — divergence is a real risk.
- **Field extraction order.** `process_alert` resolves required fields in the order: configured `fields_location` (default `commonLabels`) → `alerts[0].labels` fallback → `remediation.static_options` fallback. `value_mappings` and `job_id_mappings` then read from `source_map`, `alerts[0].labels`, or the already-processed result. Any change to this lookup chain affects every existing alert config — call it out.
- **JIRA lookup heuristics.** `find_ticket_by_summary` hardcodes `"Responsible Team" = "SRE-NOC"`, label list `("NOC","ingestion-content")`, a default `reporter_account_id`, and a 30-day window (despite the docstring claiming 1 hour). These are intentional today but are review-worthy whenever the JIRA workflow changes.
- **Domain-specific `http→https` rewrite.** `extract_alertmanager_url` upgrades scheme only for `hunters.ai` hosts. Adding new tenants/domains requires touching this; flag any new caller that bypasses it.

### What is intentional (do **not** flag as bugs)

- `config/config.go` is a leftover from the previous Go implementation and is not built or imported. Leave it alone unless explicitly asked to remove it.
- `auth_token` and `base_url` at the top level of `Config` are kept for backward compatibility with the legacy flat config; the new format is the nested `rundeck:` block. The `get_rundeck_*` helpers paper over both.
- `Workflow recovery skipping workflows older than 24 hours` and auto-escalating them is by design.
- `DynamoDB list_active` doing per-state scans (rather than a single GSI query) is intentional given the GSI shape; flag only if the state set grows materially.
- The `mock_config` fixture in `tests/conftest.py` constructs `RemediationConfig` with **non-existent** fields (`resolution_wait_minutes`, `cooldown_minutes`). Pydantic ignores unknown kwargs only if the model permits them; this fixture is known-stale and is not consumed by every test. Treat as tech debt, not a regression introduced by a current change unless the change touches it.
- `prometheus_client` Counters are module-level globals in `api/app.py`. This is the standard pattern for the library; do not refactor into per-request instances.
- The `TODO` in `clients/rundeck.py` about replacing session auth and the `TODO` next to rate-limiter init in `api/app.py` are tracked work — leave them in place.
