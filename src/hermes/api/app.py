"""
Hermes FastAPI Application.

Main application entry point with all API routes and middleware.
"""
import logging
import os
import time
import json
from urllib.parse import urlparse, urlunparse
from typing import Dict, Any, Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, status, Response
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field

from hermes.config import load_alert_config, Config
from hermes.core.state_store import InMemoryStateStore, DynamoDBStateStore
from hermes.core.remediation_manager import RemediationManager
from hermes.clients.rundeck import RundeckClient
from hermes.utils.audit_logger import get_audit_logger
from hermes.utils.rate_limiter import RateLimiter


# Filter to exclude health check logs
class HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return 'GET /health' not in message and 'HEAD /health' not in message


# Initialize logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Apply health check filter to uvicorn access logger
logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

# Prometheus Metrics
INCOMING_REQUESTS = Counter(
    "hermes_incoming_requests_total",
    "The total number of incoming alert requests"
)
WEBHOOK_REQUESTS = Counter(
    "hermes_webhook_requests_total",
    "The total number of webhook requests sent"
)
WEBHOOK_ERRORS = Counter(
    "hermes_webhook_errors_total",
    "The total number of webhook request errors"
)
PROCESSING_DURATION = Histogram(
    "hermes_processing_duration_seconds",
    "The time taken to process alert requests"
)
ALERTS_RECEIVED_BY_TYPE = Counter(
    "hermes_alerts_received_total",
    "The total number of alerts received by type",
    ["alert_type"]
)
PROCESSING_ERRORS = Counter(
    "hermes_processing_errors_total",
    "The total number of errors during alert processing",
    ["error_type", "alert_type"]
)
RUNDECK_JOB_TRIGGERS = Counter(
    "hermes_rundeck_job_triggers_total",
    "The total number of Rundeck job triggers",
    ["alert_type", "status"]
)
REMEDIATION_WORKFLOWS = Counter(
    "hermes_remediation_workflows_total",
    "The total number of remediation workflows started",
    ["alert_type"]
)
ALERTS_DEDUPLICATED = Counter(
    "hermes_alerts_deduplicated_total",
    "The total number of alerts skipped due to deduplication",
    ["alert_type", "reason"]
)
CIRCUIT_BREAKER_TRIPS = Counter(
    "hermes_circuit_breaker_trips_total",
    "The total number of circuit breaker trips",
    ["service"]
)
REMEDIATION_OUTCOMES = Counter(
    "hermes_remediation_outcomes_total",
    "The total number of remediation outcomes by result",
    ["alert_type", "outcome"]
)
RATE_LIMITED_REQUESTS = Counter(
    "hermes_rate_limited_requests_total",
    "The total number of rate limited requests",
    ["source", "limit_type"]
)

# Load Config
CONFIG_PATH = os.getenv("CONFIG_PATH", "config/config.yaml")
try:
    if not os.path.exists(CONFIG_PATH):
        CONFIG_PATH = "config/config.yaml"
    
    app_config = load_alert_config(CONFIG_PATH)
except Exception as e:
    logger.error(f"Failed to load configuration: {e}")
    app_config = None


def create_state_store(config: Config):
    """Create state store based on configuration."""
    if config.state_store.type == "dynamodb":
        return DynamoDBStateStore(
            table_name=config.state_store.dynamodb_table,
            region=config.state_store.dynamodb_region,
            endpoint_url=config.state_store.dynamodb_endpoint,
            ttl_hours=config.state_store.ttl_hours
        )
    else:
        return InMemoryStateStore()


state_store = create_state_store(app_config) if app_config else InMemoryStateStore()
rundeck_client = app_config.create_rundeck_client() if app_config else None


def record_remediation_outcome(alert_name: str, outcome: str):
    """Callback to record remediation outcomes in Prometheus metrics."""
    REMEDIATION_OUTCOMES.labels(alert_type=alert_name, outcome=outcome).inc()


remediation_manager = RemediationManager(
    app_config, 
    state_store, 
    rundeck_client,
    metrics_callback=record_remediation_outcome
) if app_config and rundeck_client else None

# Initialize rate limiter based on config
rate_limiter: Optional[RateLimiter] = None
if app_config and getattr(app_config.remediation, 'rate_limit_enabled', True):
    rate_limiter = RateLimiter(
        default_rate=getattr(app_config.remediation, 'rate_limit_per_source_rate', 10.0),
        default_burst=getattr(app_config.remediation, 'rate_limit_per_source_burst', 50),
        global_rate=getattr(app_config.remediation, 'rate_limit_global_rate', 100.0),
        global_burst=getattr(app_config.remediation, 'rate_limit_global_burst', 500)
    )
    logger.info("Rate limiter initialized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    logger.info("Hermes starting up...")
    
    # Recover active workflows from state store on startup
    if remediation_manager:
        try:
            recovered_count = await remediation_manager.recover_active_workflows()
            if recovered_count > 0:
                logger.info(f"Recovered {recovered_count} active workflows from state store")
        except Exception as e:
            logger.error(f"Failed to recover workflows on startup: {e}")
    
    yield
    # Shutdown
    logger.info("Hermes shutting down...")
    if remediation_manager:
        await remediation_manager.shutdown()


def extract_alertmanager_url(client_url: Optional[str]) -> Optional[str]:
    """
    Extract the base Alertmanager URL from the client_url field in alert payload.
    
    Example:
        Input: "http://alertmanager.eu-west-1.hunters.ai/#/alerts?receiver=pd-leads-scoring"
        Output: "http://alertmanager.eu-west-1.hunters.ai"
    
    This enables Hermes to run as a global service, receiving alerts from 
    multiple Alertmanagers and querying the correct one for alert status.
    """
    if not client_url:
        return None
    
    try:
        parsed = urlparse(client_url)
        # Reconstruct with just scheme, netloc (host:port)
        base_url = urlunparse((parsed.scheme, parsed.netloc, '', '', '', ''))
        
        if not base_url:
            return None
            
        # Convert HTTP to HTTPS for external hunters.ai URLs
        if "hunters.ai" in parsed.netloc and parsed.scheme == "http":
            parsed = parsed._replace(scheme="https")
            base_url = urlunparse((parsed.scheme, parsed.netloc, '', '', '', ''))
        
        return base_url
    except Exception as e:
        logger.warning(f"Failed to parse client_url '{client_url}': {e}")
        return None


# Initialize FastAPI
app = FastAPI(
    title="Hermes",
    description="Automated alert remediation orchestrator",
    lifespan=lifespan
)


class AlertLabel(BaseModel):
    alertname: str
    severity: Optional[str] = None


class SingleAlert(BaseModel):
    status: str
    labels: AlertLabel
    startsAt: Optional[str] = None
    endsAt: Optional[str] = None
    generatorURL: Optional[str] = None
    fingerprint: Optional[str] = None


class AlertPayload(BaseModel):
    receiver: str
    status: str
    alerts: List[SingleAlert]
    commonLabels: Dict[str, str]
    externalURL: Optional[str] = None
    version: Optional[str] = None
    groupKey: Optional[str] = None


class AlertProcessor:
    """Processes incoming alerts and triggers Rundeck jobs."""
    
    def __init__(self, config: Config, rundeck: RundeckClient):
        self.config = config
        self.rundeck = rundeck

    def process_alert(self, payload: Dict[str, Any]) -> tuple:
        alert_name = payload.get("commonLabels", {}).get("alertname")
        
        if not alert_name and payload.get("alerts"):
            alert_name = payload["alerts"][0].get("labels", {}).get("alertname")

        if not alert_name:
            PROCESSING_ERRORS.labels(error_type="missing_alertname", alert_type="unknown").inc()
            logger.error("Alert name not found in payload")
            self._raise_error("alertname not found in payload")

        ALERTS_RECEIVED_BY_TYPE.labels(alert_type=alert_name).inc()

        alert_config = self.config.get_alert_config(alert_name)
        if not alert_config:
            PROCESSING_ERRORS.labels(error_type="unknown_alert_type", alert_type=alert_name).inc()
            self._raise_error(f"no configuration found for alert: {alert_name}")

        result = {}
        missing_fields = []
        
        fields_location = alert_config.fields_location or "commonLabels"
        
        source_map = {}
        if fields_location == "root":
            source_map = payload
        else:
            source_map = payload.get(fields_location, {})
            if not isinstance(source_map, dict):
                logger.warning(f"Fields location '{fields_location}' not found or not a dict for alert '{alert_name}'")
                source_map = {}

        for field in alert_config.required_fields:
            if field in source_map:
                target_field = alert_config.field_mappings.get(field, field)
                logger.info(f"Mapping field '{field}' -> '{target_field}' with value '{source_map[field]}'")
                result[target_field] = source_map[field]
            else:
                logger.warning(f"Required field '{field}' not found in location '{fields_location}' for alert '{alert_name}'")
                missing_fields.append(field)

        if missing_fields:
            PROCESSING_ERRORS.labels(error_type="missing_fields", alert_type=alert_name).inc()
            logger.warning(f"Missing required fields {missing_fields} for alert '{alert_name}'")
        
        return result, alert_name, missing_fields

    async def send_to_webhook(self, alert_name: str, payload: Dict[str, Any], alert_time: str) -> Dict[str, Any]:
        """Send to Rundeck and return execution details."""
        alert_config = self.config.get_alert_config(alert_name)
        if not alert_config:
            PROCESSING_ERRORS.labels(error_type="config_not_found", alert_type=alert_name).inc()
            self._raise_error(f"no configuration found for alert: {alert_name}")

        logger.info(f"Sending to Rundeck job {alert_config.job_id} with options: {json.dumps(payload, indent=2)}")
        
        try:
            response_data = await self.rundeck.run_job(
                job_id=alert_config.job_id,
                options=payload
            )
        except Exception as e:
            WEBHOOK_ERRORS.inc()
            RUNDECK_JOB_TRIGGERS.labels(alert_type=alert_name, status="error").inc()
            logger.error(f"Error sending to Rundeck: {e}")
            raise Exception(f"Rundeck job trigger failed: {e}")

        WEBHOOK_REQUESTS.inc()
        RUNDECK_JOB_TRIGGERS.labels(alert_type=alert_name, status="success").inc()
        
        return {
            "execution_id": str(response_data.get("id", "")),
            "execution_url": response_data.get("permalink") or response_data.get("href", "")
        }

    def _raise_error(self, message: str):
        raise ValueError(message)


processor = AlertProcessor(app_config, rundeck_client) if app_config and rundeck_client else None


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    if request.url.path == "/api/v1/alerts":
        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time
        PROCESSING_DURATION.observe(process_time)
        return response
    return await call_next(request)


@app.get("/metrics")
async def metrics():
    return Response(
        content=generate_latest(), 
        media_type=CONTENT_TYPE_LATEST
    )


@app.get("/health")
async def health():
    """Basic health check endpoint - always returns healthy if service is running."""
    return {"status": "healthy", "remediation_enabled": app_config.is_remediation_enabled() if app_config else False}


@app.get("/health/ready")
async def health_ready():
    """
    Readiness probe that checks external dependencies.
    
    Returns 503 if any critical dependency is unhealthy.
    """
    health_status = {
        "status": "healthy",
        "config_loaded": app_config is not None,
        "dependencies": {}
    }
    
    is_healthy = True
    
    # Check DynamoDB state store
    if app_config and app_config.state_store.type == "dynamodb":
        try:
            await state_store.get("__health_check__")
            health_status["dependencies"]["dynamodb"] = "healthy"
        except Exception as e:
            health_status["dependencies"]["dynamodb"] = f"unhealthy: {str(e)[:100]}"
            is_healthy = False
    else:
        health_status["dependencies"]["dynamodb"] = "not_configured"
    
    # Check circuit breaker states
    if remediation_manager:
        circuit_states = {}
        for service, cb in remediation_manager._circuit_breakers.items():
            if cb.is_open:
                circuit_states[service] = "open"
                is_healthy = False
            else:
                circuit_states[service] = "closed"
        health_status["circuit_breakers"] = circuit_states
        
        # Include active workflow count
        health_status["active_workflows"] = len(remediation_manager._running_tasks)
        health_status["max_workflows"] = remediation_manager.max_concurrent_workflows
    
    if not is_healthy:
        health_status["status"] = "unhealthy"
        return JSONResponse(status_code=503, content=health_status)
    
    return health_status


@app.get("/health/live")
async def health_live():
    """
    Liveness probe - simple check that the service is running.
    """
    return {"status": "alive"}


@app.post("/api/v1/alerts")
async def receive_alert(request: Request):
    INCOMING_REQUESTS.inc()
    
    if not processor:
        raise HTTPException(status_code=500, detail="Configuration not loaded")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        PROCESSING_ERRORS.labels(error_type="invalid_json", alert_type="unknown").inc()
        logger.error("Error parsing alert payload: Invalid JSON")
        raise HTTPException(status_code=400, detail="Invalid alert payload")

    # Handle resolved events from Alertmanager (send_resolved: true)
    if body.get("status") == "resolved" and remediation_manager:
        resolved_count = 0
        for alert in body.get("alerts", []):
            if alert.get("status") == "resolved":
                alert_name = alert.get("labels", {}).get("alertname")
                alert_labels = alert.get("labels", {})
                if alert_name:
                    workflow_id = await remediation_manager.handle_resolved_event(
                        alert_name, alert_labels
                    )
                    if workflow_id:
                        resolved_count += 1
                        logger.info(f"Processed resolved event for {alert_name}, workflow: {workflow_id}")
        
        return {
            "status": "success", 
            "message": "Resolved event processed",
            "workflows_signaled": resolved_count
        }

    try:
        processed_alert, alert_name, missing_fields = processor.process_alert(body)
    except ValueError as e:
        logger.error(f"Error processing alert: {e}")
        if "no configuration found" in str(e):
            return JSONResponse(status_code=500, content={"error": f"Error processing alert: {e}"})
        return JSONResponse(status_code=500, content={"error": str(e)})

    logger.info(f"Received alert: {alert_name}")
    
    if os.getenv("DEBUG") == "true":
        logger.info(f"Full alert payload for '{alert_name}':\n{json.dumps(body, indent=2)}")

    if missing_fields:
        error_msg = f"Missing required fields for Rundeck job: {missing_fields}"
        logger.error(error_msg)
        return JSONResponse(
            status_code=400, 
            content={
                "error": error_msg,
                "available_fields": processed_alert
            }
        )

    # Check rate limiting before processing
    alertmanager_source = body.get("externalURL") or body.get("client_url") or "unknown"
    if rate_limiter:
        allowed, rate_limit_reason = await rate_limiter.try_acquire(alertmanager_source)
        if not allowed:
            limit_type = "global" if "Global" in (rate_limit_reason or "") else "per_source"
            RATE_LIMITED_REQUESTS.labels(source=alertmanager_source[:50], limit_type=limit_type).inc()
            logger.warning(f"Rate limited alert '{alert_name}' from {alertmanager_source}: {rate_limit_reason}")
            return JSONResponse(
                status_code=429,
                content={
                    "status": "rate_limited",
                    "message": rate_limit_reason,
                    "alert_name": alert_name,
                    "source": alertmanager_source
                }
            )

    alert_time = str(time.time())
    alerts_list = body.get("alerts", [])
    if alerts_list and alerts_list[0].get("startsAt"):
        alert_time = alerts_list[0].get("startsAt")

    # Check deduplication before triggering Rundeck
    alert_labels = body.get("commonLabels", {}).copy()
    alertmanager_url = extract_alertmanager_url(body.get("externalURL") or body.get("client_url"))
    
    # Audit log - alert received
    audit_logger = get_audit_logger()
    audit_logger.log_alert_received(alert_name, alert_labels, alertmanager_url)
    
    if remediation_manager:
        should_skip, skip_reason, existing_workflow_id = await remediation_manager.check_deduplication(
            alert_name, alert_labels
        )
        if should_skip:
            reason_label = "active_workflow" if existing_workflow_id else "cooldown_or_limit"
            ALERTS_DEDUPLICATED.labels(alert_type=alert_name, reason=reason_label).inc()
            logger.info(f"Alert '{alert_name}' deduplicated: {skip_reason}")
            
            # Audit log - deduplication
            audit_logger.log_alert_deduplicated(alert_name, alert_labels, skip_reason, existing_workflow_id)
            
            return {
                "status": "deduplicated",
                "message": skip_reason,
                "existing_workflow_id": existing_workflow_id
            }
        
        # Check circuit breaker for Rundeck
        is_allowed, cb_reason = remediation_manager.check_circuit_breaker("rundeck")
        if not is_allowed:
            CIRCUIT_BREAKER_TRIPS.labels(service="rundeck").inc()
            logger.warning(f"Rundeck circuit breaker open for alert '{alert_name}': {cb_reason}")
            return JSONResponse(
                status_code=503,
                content={
                    "status": "circuit_breaker_open",
                    "message": cb_reason,
                    "alert_name": alert_name
                }
            )

    try:
        execution_info = await processor.send_to_webhook(alert_name, processed_alert, alert_time)
        # Record success for circuit breaker
        if remediation_manager:
            remediation_manager.record_circuit_success("rundeck")
    except Exception as e:
        # Record failure for circuit breaker
        if remediation_manager:
            remediation_manager.record_circuit_failure("rundeck")
        logger.error(f"Error sending to webhook: {e}")
        logger.error(f"Failed to send processed payload for alert '{alert_name}':\n{json.dumps(processed_alert, indent=2)}")
        return JSONResponse(status_code=500, content={"error": f"Error sending to webhook: {e}"})

    logger.info(f"Sent alert to webhook: {alert_name}")
    
    # Start remediation tracking if enabled
    workflow_id = None
    if remediation_manager and app_config.is_remediation_enabled():
        try:
            workflow_id = await remediation_manager.start_remediation(
                alert_name=alert_name,
                alert_labels=alert_labels,
                rundeck_execution_id=execution_info["execution_id"],
                rundeck_execution_url=execution_info["execution_url"],
                alertmanager_url=alertmanager_url
            )
            REMEDIATION_WORKFLOWS.labels(alert_type=alert_name).inc()
            logger.info(f"Started remediation workflow: {workflow_id} (AM: {alertmanager_url})")
            
            # Audit log - workflow started
            audit_logger.log_workflow_started(
                workflow_id, alert_name, alert_labels,
                execution_info["execution_id"], alertmanager_url
            )
        except Exception as e:
            # Don't fail the request if remediation tracking fails
            logger.error(f"Failed to start remediation tracking: {e}")
    
    return {
        "status": "success", 
        "message": "Alert processed and sent to webhook",
        "execution_id": execution_info.get("execution_id"),
        "workflow_id": workflow_id
    }


@app.get("/api/v1/remediations")
async def list_remediations():
    """List all active remediation workflows."""
    if not remediation_manager:
        raise HTTPException(status_code=503, detail="Remediation manager not available")
    
    workflows = await remediation_manager.list_active_workflows()
    return {"workflows": workflows}


@app.get("/api/v1/remediations/{workflow_id}")
async def get_remediation(workflow_id: str):
    """Get status of a specific remediation workflow."""
    if not remediation_manager:
        raise HTTPException(status_code=503, detail="Remediation manager not available")
    
    workflow = await remediation_manager.get_workflow_status(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    
    return workflow


@app.get("/api/v1/rate-limits")
async def get_rate_limits():
    """Get rate limiter statistics for debugging."""
    if not rate_limiter:
        return {"enabled": False, "message": "Rate limiting not configured"}
    
    stats = rate_limiter.get_stats()
    return {
        "enabled": True,
        "stats": stats
    }


def run_server():
    """Run the server with uvicorn."""
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    logger.info(f"Starting server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    run_server()
