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
import httpx
from pydantic import BaseModel, Field

from config import load_alert_config, Config
from state_store import InMemoryStateStore
from remediation_manager import RemediationManager
from rundeck_client import RundeckClient

# Initialize logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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

# Load Config
CONFIG_PATH = os.getenv("CONFIG_PATH", "config/config.yaml")
try:
    if not os.path.exists(CONFIG_PATH):
        CONFIG_PATH = "config/config.yaml"
    
    app_config = load_alert_config(CONFIG_PATH)
except Exception as e:
    logger.error(f"Failed to load configuration: {e}")
    app_config = None

# Initialize State Store based on config
def create_state_store(config):
    """Create state store based on configuration."""
    if config.state_store.type == "dynamodb":
        from state_store import DynamoDBStateStore
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
remediation_manager = RemediationManager(app_config, state_store, rundeck_client) if app_config and rundeck_client else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    logger.info("Hermes starting up...")
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
        return base_url if base_url else None
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
    def __init__(self, config: Config, rundeck: RundeckClient):
        self.config = config
        self.rundeck = rundeck

    def process_alert(self, payload: Dict[str, Any]) -> Dict[str, Any]:
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

        try:
            # Use RundeckClient which handles token or session-based auth
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
        
        # Return execution details for remediation tracking
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
    """Health check endpoint."""
    return {"status": "healthy", "remediation_enabled": app_config.is_remediation_enabled() if app_config else False}


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

    alert_time = str(time.time())
    alerts_list = body.get("alerts", [])
    if alerts_list and alerts_list[0].get("startsAt"):
         alert_time = alerts_list[0].get("startsAt")

    try:
        execution_info = await processor.send_to_webhook(alert_name, processed_alert, alert_time)
    except Exception as e:
        logger.error(f"Error sending to webhook: {e}")
        logger.error(f"Failed to send processed payload for alert '{alert_name}':\n{json.dumps(processed_alert, indent=2)}")
        return JSONResponse(status_code=500, content={"error": f"Error sending to webhook: {e}"})

    logger.info(f"Sent alert to webhook: {alert_name}")
    
    # Start remediation tracking if enabled
    workflow_id = None
    if remediation_manager and app_config.is_remediation_enabled():
        try:
            # Get alert labels for matching later
            alert_labels = body.get("commonLabels", {}).copy()
            
            # Extract Alertmanager URL from client_url for global service mode
            # client_url format: "http://alertmanager.eu-west-1.hunters.ai/#/alerts?receiver=..."
            alertmanager_url = extract_alertmanager_url(body.get("client_url"))
            
            workflow_id = await remediation_manager.start_remediation(
                alert_name=alert_name,
                alert_labels=alert_labels,
                rundeck_execution_id=execution_info["execution_id"],
                rundeck_execution_url=execution_info["execution_url"],
                alertmanager_url=alertmanager_url
            )
            REMEDIATION_WORKFLOWS.labels(alert_type=alert_name).inc()
            logger.info(f"Started remediation workflow: {workflow_id} (AM: {alertmanager_url})")
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


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    logger.info(f"Starting server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
