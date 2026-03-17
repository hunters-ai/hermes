"""State store for tracking remediation workflows."""
import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)


class RemediationState(str, Enum):
    """States in the remediation lifecycle."""
    RECEIVED = "received"
    JOB_TRIGGERED = "job_triggered"
    JOB_RUNNING = "job_running"
    JOB_SUCCEEDED = "job_succeeded"
    JOB_FAILED = "job_failed"
    WAITING_RESOLUTION = "waiting_resolution"
    ALERT_RESOLVED = "alert_resolved"
    ALERT_STILL_FIRING = "alert_still_firing"
    ESCALATED = "escalated"
    COMPLETED = "completed"


@dataclass
class RemediationWorkflow:
    """Represents a single remediation workflow instance."""
    id: str
    alert_name: str
    alert_labels: Dict[str, str]
    state: RemediationState
    rundeck_execution_id: Optional[str] = None
    rundeck_execution_url: Optional[str] = None
    jira_ticket_id: Optional[str] = None
    alertmanager_url: Optional[str] = None  # Source Alertmanager URL from client_url
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    job_completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    attempts: int = 0
    last_triggered_at: Optional[datetime] = None
    rundeck_options: Optional[Dict[str, str]] = None
    
    def update_state(self, new_state: RemediationState, error: Optional[str] = None):
        """Update workflow state."""
        self.state = new_state
        self.updated_at = datetime.utcnow()
        if error:
            self.error_message = error


class StateStore(ABC):
    """Abstract base class for state persistence."""
    
    @abstractmethod
    async def save(self, workflow: RemediationWorkflow) -> None:
        """Save or update a workflow."""
        pass
    
    @abstractmethod
    async def get(self, workflow_id: str) -> Optional[RemediationWorkflow]:
        """Get a workflow by ID."""
        pass
    
    @abstractmethod
    async def get_by_execution_id(self, execution_id: str) -> Optional[RemediationWorkflow]:
        """Get a workflow by Rundeck execution ID."""
        pass
    
    @abstractmethod
    async def list_active(self) -> List[RemediationWorkflow]:
        """List all active (non-completed) workflows."""
        pass
    
    @abstractmethod
    async def get_by_alert_labels(
        self, 
        alert_name: str, 
        alert_labels: Dict[str, str]
    ) -> Optional[RemediationWorkflow]:
        """Get active workflow matching the alert name and labels."""
        pass
    
    @abstractmethod
    async def delete(self, workflow_id: str) -> None:
        """Delete a workflow."""
        pass


class InMemoryStateStore(StateStore):
    """In-memory state store implementation.
    
    Note: State is lost on pod restart. Suitable for development/testing.
    For production, use DynamoDBStateStore.
    """
    
    def __init__(self):
        self._workflows: Dict[str, RemediationWorkflow] = {}
        self._lock = asyncio.Lock()
    
    async def save(self, workflow: RemediationWorkflow) -> None:
        async with self._lock:
            self._workflows[workflow.id] = workflow
    
    async def get(self, workflow_id: str) -> Optional[RemediationWorkflow]:
        return self._workflows.get(workflow_id)
    
    async def get_by_execution_id(self, execution_id: str) -> Optional[RemediationWorkflow]:
        for workflow in self._workflows.values():
            if workflow.rundeck_execution_id == execution_id:
                return workflow
        return None
    
    async def list_active(self) -> List[RemediationWorkflow]:
        completed_states = {RemediationState.COMPLETED, RemediationState.ALERT_RESOLVED}
        return [
            w for w in self._workflows.values()
            if w.state not in completed_states
        ]
    
    async def get_by_alert_labels(
        self, 
        alert_name: str, 
        alert_labels: Dict[str, str]
    ) -> Optional[RemediationWorkflow]:
        """Get active workflow matching the alert name and labels."""
        completed_states = {RemediationState.COMPLETED, RemediationState.ALERT_RESOLVED, RemediationState.ESCALATED}
        for workflow in self._workflows.values():
            if workflow.state in completed_states:
                continue
            if workflow.alert_name != alert_name:
                continue
            # Check if all alert_labels match
            if all(workflow.alert_labels.get(k) == v for k, v in alert_labels.items()):
                return workflow
        return None
    
    async def delete(self, workflow_id: str) -> None:
        async with self._lock:
            self._workflows.pop(workflow_id, None)
    
    async def cleanup_old(self, max_age_hours: int = 24) -> int:
        """Remove completed workflows older than max_age_hours."""
        async with self._lock:
            now = datetime.utcnow()
            to_delete = []
            for wf_id, wf in self._workflows.items():
                if wf.state in {RemediationState.COMPLETED, RemediationState.ALERT_RESOLVED}:
                    age = (now - wf.updated_at).total_seconds() / 3600
                    if age > max_age_hours:
                        to_delete.append(wf_id)
            
            for wf_id in to_delete:
                del self._workflows[wf_id]
            
            return len(to_delete)


class DynamoDBStateStore(StateStore):
    """DynamoDB-backed state store implementation.
    
    Required DynamoDB table setup:
    - Table name: configurable (default: hermes-workflows)
    - Partition key: id (String)
    - GSI: execution_id-index on rundeck_execution_id (String)
    - GSI: alert_name-state-index on alert_name (String) and state (String)
    - TTL attribute: ttl (Number - Unix timestamp)
    """
    
    EXECUTION_ID_INDEX = "execution_id-index"
    ALERT_NAME_STATE_INDEX = "alert_name-state-index"
    
    def __init__(
        self, 
        table_name: str = "hunters-ops-hermes-workflows",
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        ttl_hours: int = 24
    ):
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError:
            raise ImportError("boto3 is required for DynamoDBStateStore. Run: pip install boto3")
        
        self.table_name = table_name
        self.ttl_hours = ttl_hours
        
        config = BotoConfig(retries={'max_attempts': 3, 'mode': 'adaptive'})
        session = boto3.Session(region_name=region) if region else boto3.Session()
        
        if endpoint_url:
            self.dynamodb = session.resource('dynamodb', config=config, endpoint_url=endpoint_url)
            logger.info(f"Initialized DynamoDB state store with endpoint: {endpoint_url}")
        else:
            self.dynamodb = session.resource('dynamodb', config=config)
        
        self.table = self.dynamodb.Table(table_name)
        logger.info(f"Initialized DynamoDB state store with table: {table_name}")
    
    def _workflow_to_item(self, workflow: RemediationWorkflow) -> Dict:
        """Convert workflow to DynamoDB item."""
        ttl_timestamp = int((datetime.utcnow().timestamp()) + (self.ttl_hours * 3600))
        
        item = {
            "id": workflow.id,
            "alert_name": workflow.alert_name,
            "alert_labels": workflow.alert_labels,
            "state": workflow.state.value,
            "created_at": workflow.created_at.isoformat(),
            "updated_at": workflow.updated_at.isoformat(),
            "ttl": ttl_timestamp
        }
        
        if workflow.rundeck_execution_id:
            item["rundeck_execution_id"] = workflow.rundeck_execution_id
        if workflow.rundeck_execution_url:
            item["rundeck_execution_url"] = workflow.rundeck_execution_url
        if workflow.jira_ticket_id:
            item["jira_ticket_id"] = workflow.jira_ticket_id
        if workflow.job_completed_at:
            item["job_completed_at"] = workflow.job_completed_at.isoformat()
        if workflow.error_message:
            item["error_message"] = workflow.error_message
        if workflow.alertmanager_url:
            item["alertmanager_url"] = workflow.alertmanager_url
        
        item["attempts"] = workflow.attempts
        if workflow.last_triggered_at:
            item["last_triggered_at"] = workflow.last_triggered_at.isoformat()
        if workflow.rundeck_options:
            item["rundeck_options"] = workflow.rundeck_options
        
        return item
    
    def _item_to_workflow(self, item: Dict) -> RemediationWorkflow:
        """Convert DynamoDB item to workflow."""
        return RemediationWorkflow(
            id=item["id"],
            alert_name=item["alert_name"],
            alert_labels=item.get("alert_labels", {}),
            state=RemediationState(item["state"]),
            rundeck_execution_id=item.get("rundeck_execution_id"),
            rundeck_execution_url=item.get("rundeck_execution_url"),
            jira_ticket_id=item.get("jira_ticket_id"),
            created_at=datetime.fromisoformat(item["created_at"]),
            updated_at=datetime.fromisoformat(item["updated_at"]),
            job_completed_at=datetime.fromisoformat(item["job_completed_at"]) if item.get("job_completed_at") else None,
            error_message=item.get("error_message"),
            alertmanager_url=item.get("alertmanager_url"),
            attempts=int(item.get("attempts", 0)),
            last_triggered_at=datetime.fromisoformat(item["last_triggered_at"]) if item.get("last_triggered_at") else None,
            rundeck_options=item.get("rundeck_options")
        )
    
    async def save(self, workflow: RemediationWorkflow) -> None:
        """Save or update a workflow."""
        item = self._workflow_to_item(workflow)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self.table.put_item(Item=item))
    
    async def get(self, workflow_id: str) -> Optional[RemediationWorkflow]:
        """Get a workflow by ID."""
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, 
            lambda: self.table.get_item(Key={"id": workflow_id})
        )
        item = response.get("Item")
        return self._item_to_workflow(item) if item else None
    
    async def get_by_execution_id(self, execution_id: str) -> Optional[RemediationWorkflow]:
        """Get a workflow by Rundeck execution ID using GSI."""
        from boto3.dynamodb.conditions import Key
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self.table.query(
                IndexName=self.EXECUTION_ID_INDEX,
                KeyConditionExpression=Key("rundeck_execution_id").eq(execution_id)
            )
        )
        items = response.get("Items", [])
        return self._item_to_workflow(items[0]) if items else None
    
    async def list_active(self) -> List[RemediationWorkflow]:
        """List all active (non-completed) workflows."""
        from boto3.dynamodb.conditions import Attr
        
        loop = asyncio.get_event_loop()
        completed_states = [RemediationState.COMPLETED.value, RemediationState.ALERT_RESOLVED.value]
        active_states = [s.value for s in RemediationState if s.value not in completed_states]
        
        all_workflows = []
        try:
            for state in active_states:
                response = await loop.run_in_executor(
                    None,
                    lambda s=state: self.table.scan(FilterExpression=Attr("state").eq(s))
                )
                all_workflows.extend(response.get("Items", []))
        except Exception as e:
            logger.warning(f"GSI query failed, falling back to scan: {e}")
            response = await loop.run_in_executor(
                None,
                lambda: self.table.scan(FilterExpression=Attr("state").is_in(active_states))
            )
            all_workflows = response.get("Items", [])
        
        return [self._item_to_workflow(item) for item in all_workflows]
    
    async def get_by_alert_labels(
        self, 
        alert_name: str, 
        alert_labels: Dict[str, str]
    ) -> Optional[RemediationWorkflow]:
        """Get active workflow matching the alert name and labels."""
        from boto3.dynamodb.conditions import Key, Attr
        
        loop = asyncio.get_event_loop()
        completed_states = [
            RemediationState.COMPLETED.value, 
            RemediationState.ALERT_RESOLVED.value,
            RemediationState.ESCALATED.value
        ]
        active_states = [s.value for s in RemediationState if s.value not in completed_states]
        
        # Query the GSI for each active state since state is a key attribute
        # Cannot use FilterExpression on GSI key attributes
        items = []
        try:
            for state in active_states:
                response = await loop.run_in_executor(
                    None,
                    lambda s=state: self.table.query(
                        IndexName=self.ALERT_NAME_STATE_INDEX,
                        KeyConditionExpression=Key("alert_name").eq(alert_name) & Key("state").eq(s)
                    )
                )
                items.extend(response.get("Items", []))
        except Exception as e:
            logger.warning(f"GSI query failed for alert_name '{alert_name}', falling back to scan: {e}")
            response = await loop.run_in_executor(
                None,
                lambda: self.table.scan(
                    FilterExpression=(
                        Attr("alert_name").eq(alert_name) &
                        Attr("state").is_in(active_states)
                    )
                )
            )
            items = response.get("Items", [])
        
        for item in items:
            workflow = self._item_to_workflow(item)
            if all(workflow.alert_labels.get(k) == v for k, v in alert_labels.items()):
                return workflow
        return None
    
    async def delete(self, workflow_id: str) -> None:
        """Delete a workflow."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self.table.delete_item(Key={"id": workflow_id})
        )
