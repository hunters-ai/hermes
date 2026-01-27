"""Remediation manager - orchestrates the full remediation lifecycle."""
import asyncio
import logging
import uuid
from typing import Dict, Any, Optional
from datetime import datetime

from config import Config
from state_store import StateStore, RemediationWorkflow, RemediationState
from job_monitor import JobMonitor
from alertmanager_client import AlertmanagerClient
from rundeck_client import RundeckClient
from jira_client import JiraClient
from slack_client import SlackClient

logger = logging.getLogger(__name__)


class RemediationManager:
    """
    Orchestrates the full remediation workflow:
    1. Track triggered Rundeck jobs
    2. Poll for job completion
    3. Wait configured time after success
    4. Check if alert is resolved via Alertmanager
    5. Update JIRA with results
    6. Escalate to Slack if remediation failed
    """
    
    def __init__(self, config: Config, state_store: StateStore, rundeck_client: RundeckClient):
        self.config = config
        self.state_store = state_store
        
        # Use shared client
        self.job_monitor = JobMonitor(rundeck_client)
        
        self.alertmanager_client = None
        if config.alertmanager:
            self.alertmanager_client = AlertmanagerClient(
                config.alertmanager.base_url,
                config.alertmanager.bearer_token
            )
        
        self.jira_client = None
        if config.jira:
            self.jira_client = JiraClient(
                config.jira.base_url,
                config.jira.user_email,
                config.jira.api_token
            )
        
        self.slack_client = None
        if config.slack:
            self.slack_client = SlackClient(
                webhook_url=config.slack.webhook_url,
                bot_token=config.slack.bot_token,
                noc_channel=config.slack.noc_channel,
                noc_user_group=config.slack.noc_user_group
            )
        
        # Background task tracking
        self._running_tasks: Dict[str, asyncio.Task] = {}
        # Track resolution events for early termination via webhook
        self._resolution_events: Dict[str, asyncio.Event] = {}
    
    async def start_remediation(
        self,
        alert_name: str,
        alert_labels: Dict[str, str],
        rundeck_execution_id: str,
        rundeck_execution_url: Optional[str] = None,
        alertmanager_url: Optional[str] = None
    ) -> str:
        """
        Start tracking a remediation workflow.
        
        Called after successfully triggering a Rundeck job.
        
        Args:
            alert_name: Name of the alert
            alert_labels: Labels from the alert for matching
            rundeck_execution_id: Rundeck execution ID
            rundeck_execution_url: URL to the Rundeck execution
            alertmanager_url: Base URL of the source Alertmanager (for global service mode)
        
        Returns the workflow ID.
        """
        workflow_id = str(uuid.uuid4())
        
        workflow = RemediationWorkflow(
            id=workflow_id,
            alert_name=alert_name,
            alert_labels=alert_labels,
            state=RemediationState.JOB_TRIGGERED,
            rundeck_execution_id=rundeck_execution_id,
            rundeck_execution_url=rundeck_execution_url,
            alertmanager_url=alertmanager_url
        )
        
        await self.state_store.save(workflow)
        logger.info(f"Started remediation workflow {workflow_id} for alert {alert_name}")
        
        # Start background monitoring task
        task = asyncio.create_task(self._monitor_workflow(workflow_id))
        self._running_tasks[workflow_id] = task
        
        return workflow_id
    
    async def _monitor_workflow(self, workflow_id: str) -> None:
        """Background task to monitor a single remediation workflow."""
        # Create resolution event for this workflow
        resolution_event = asyncio.Event()
        self._resolution_events[workflow_id] = resolution_event
        
        try:
            workflow = await self.state_store.get(workflow_id)
            if not workflow:
                logger.error(f"Workflow {workflow_id} not found")
                return
            
            # Get alert-specific config
            alert_config = self.config.get_alert_config(workflow.alert_name)
            resolution_wait = self.config.remediation.resolution_wait_minutes
            if alert_config and alert_config.remediation.resolution_wait_minutes:
                resolution_wait = alert_config.remediation.resolution_wait_minutes
            
            # Step 1: Wait for job completion
            job_success = await self._wait_for_job_completion(workflow)
            
            if not job_success:
                # Job failed - escalate immediately
                await self._handle_job_failure(workflow)
                return
            
            # Step 2: Wait for alert resolution (with polling and early resolution support)
            logger.info(f"Job succeeded, waiting up to {resolution_wait} minutes for alert resolution")
            workflow.update_state(RemediationState.WAITING_RESOLUTION)
            await self.state_store.save(workflow)
            
            alert_resolved = await self._wait_for_alert_resolution(workflow, resolution_event, resolution_wait)
            
            if alert_resolved:
                await self._handle_success(workflow)
            else:
                await self._handle_alert_still_firing(workflow)
                
        except asyncio.CancelledError:
            logger.info(f"Workflow {workflow_id} monitoring cancelled")
        except Exception as e:
            logger.exception(f"Error in workflow {workflow_id}: {e}")
            workflow = await self.state_store.get(workflow_id)
            if workflow:
                workflow.update_state(RemediationState.ESCALATED, str(e))
                await self.state_store.save(workflow)
        finally:
            self._running_tasks.pop(workflow_id, None)
            self._resolution_events.pop(workflow_id, None)
    
    async def _wait_for_alert_resolution(
        self, 
        workflow: RemediationWorkflow,
        resolution_event: asyncio.Event,
        resolution_wait_minutes: int
    ) -> bool:
        """
        Wait for alert resolution via polling or early resolution webhook.
        
        Args:
            workflow: The remediation workflow
            resolution_event: Event that gets set when a resolved webhook arrives
            resolution_wait_minutes: Maximum time to wait for resolution
        
        Returns:
            True if alert resolved, False if timeout reached
        """
        check_interval = self.config.remediation.alert_check_interval_seconds
        max_wait = resolution_wait_minutes * 60
        elapsed = 0
        
        while elapsed < max_wait:
            # Check if we received a resolved webhook
            if resolution_event.is_set():
                logger.info(f"Early resolution via webhook for {workflow.id}")
                return True
            
            # Poll Alertmanager
            if await self._check_alert_resolved(workflow):
                logger.info(f"Alert resolved (confirmed by polling) for {workflow.id}")
                return True
            
            # Wait for next check interval or early resolution
            try:
                await asyncio.wait_for(
                    resolution_event.wait(), 
                    timeout=check_interval
                )
                # Event was set - resolved via webhook
                logger.info(f"Early resolution via webhook for {workflow.id}")
                return True
            except asyncio.TimeoutError:
                elapsed += check_interval
        
        return False  # Timeout - alert still firing
    
    async def handle_resolved_event(
        self, 
        alert_name: str, 
        alert_labels: Dict[str, str]
    ) -> Optional[str]:
        """
        Handle incoming resolved event from Alertmanager webhook.
        
        Called when Alertmanager sends a webhook with status=resolved.
        Finds matching active workflow and signals early resolution.
        
        Args:
            alert_name: Name of the resolved alert
            alert_labels: Labels from the resolved alert for matching
        
        Returns:
            Workflow ID if found and signaled, None otherwise
        """
        try:
            workflow = await self.state_store.get_by_alert_labels(alert_name, alert_labels)
            if workflow and workflow.id in self._resolution_events:
                logger.info(f"Received resolved event for workflow {workflow.id} (alert: {alert_name})")
                self._resolution_events[workflow.id].set()  # Signal early resolution
                return workflow.id
            else:
                return None
        except Exception as e:
            logger.error(f"Error handling resolved event: {e}")
            return None
    
    async def _wait_for_job_completion(self, workflow: RemediationWorkflow) -> bool:
        """Poll Rundeck until job completes. Returns True if succeeded."""
        poll_interval = self.config.remediation.poll_interval_seconds
        max_wait = self.config.remediation.max_job_wait_minutes * 60
        elapsed = 0
        
        workflow.update_state(RemediationState.JOB_RUNNING)
        await self.state_store.save(workflow)
        
        while elapsed < max_wait:
            try:
                is_complete, status = await self.job_monitor.is_job_complete(
                    workflow.rundeck_execution_id
                )
                
                if is_complete:
                    workflow.job_completed_at = datetime.utcnow()
                    
                    # Fetch JIRA ticket ID from execution options
                    options = await self.job_monitor.get_execution_options(
                        workflow.rundeck_execution_id
                    )
                    
                    # Get the option name for JIRA ticket (configurable per alert)
                    alert_config = self.config.get_alert_config(workflow.alert_name)
                    ticket_option = "jira_ticket"
                    if alert_config:
                        ticket_option = alert_config.remediation.jira_ticket_option
                    
                    workflow.jira_ticket_id = options.get(ticket_option)
                    
                    if status == "succeeded":
                        workflow.update_state(RemediationState.JOB_SUCCEEDED)
                        await self.state_store.save(workflow)
                        logger.info(f"Job {workflow.rundeck_execution_id} succeeded")
                        return True
                    else:
                        workflow.update_state(
                            RemediationState.JOB_FAILED, 
                            f"Job status: {status}"
                        )
                        await self.state_store.save(workflow)
                        logger.warning(f"Job {workflow.rundeck_execution_id} failed: {status}")
                        return False
                
            except Exception as e:
                logger.warning(f"Error polling job status: {e}")
            
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        
        # Timeout
        workflow.update_state(RemediationState.JOB_FAILED, "Job execution timeout")
        await self.state_store.save(workflow)
        logger.warning(f"Job {workflow.rundeck_execution_id} timed out")
        return False
    
    async def _check_alert_resolved(self, workflow: RemediationWorkflow) -> bool:
        """
        Check if the alert is no longer firing.
        
        Uses the workflow's stored alertmanager_url to query the correct Alertmanager
        instance (global service mode), falling back to the configured default.
        """
        # Determine which Alertmanager to query
        alertmanager_url = workflow.alertmanager_url
        
        if alertmanager_url:
            # Use the Alertmanager that sent this alert (global service mode)
            from alertmanager_client import AlertmanagerClient
            am_client = AlertmanagerClient(alertmanager_url)
        elif self.alertmanager_client:
            # Fall back to configured default
            am_client = self.alertmanager_client
        else:
            logger.warning("No Alertmanager available, assuming alert resolved")
            return True
        
        try:
            # Use stored alert labels to match the specific alert
            is_firing = await am_client.is_alert_firing(
                workflow.alert_name,
                workflow.alert_labels
            )
            return not is_firing
        except Exception as e:
            logger.error(f"Error checking alert status from {alertmanager_url or 'default'}: {e}")
            # On error, assume not resolved and escalate
            return False
    
    async def _handle_success(self, workflow: RemediationWorkflow) -> None:
        """Handle successful remediation - alert resolved."""
        workflow.update_state(RemediationState.ALERT_RESOLVED)
        await self.state_store.save(workflow)
        
        logger.info(f"Remediation successful for {workflow.alert_name}")
        
        # Update JIRA
        if self.jira_client and workflow.jira_ticket_id:
            try:
                await self.jira_client.add_remediation_success_comment(
                    workflow.jira_ticket_id,
                    workflow.alert_name,
                    workflow.rundeck_execution_url
                )
            except Exception as e:
                logger.error(f"Failed to update JIRA: {e}")
        
        workflow.update_state(RemediationState.COMPLETED)
        await self.state_store.save(workflow)
    
    async def _handle_alert_still_firing(self, workflow: RemediationWorkflow) -> None:
        """Handle case where job succeeded but alert is still firing."""
        workflow.update_state(RemediationState.ALERT_STILL_FIRING)
        await self.state_store.save(workflow)
        
        reason = "Rundeck job completed successfully but alert is still firing"
        logger.warning(f"Remediation incomplete for {workflow.alert_name}: {reason}")
        
        await self._escalate(workflow, reason)
    
    async def _handle_job_failure(self, workflow: RemediationWorkflow) -> None:
        """Handle Rundeck job failure."""
        reason = workflow.error_message or "Rundeck job execution failed"
        logger.warning(f"Job failed for {workflow.alert_name}: {reason}")
        
        await self._escalate(workflow, reason)
    
    async def _escalate(self, workflow: RemediationWorkflow, reason: str) -> None:
        """Escalate to JIRA and Slack."""
        # Add JIRA comment
        if self.jira_client and workflow.jira_ticket_id:
            try:
                if workflow.state == RemediationState.JOB_FAILED:
                    await self.jira_client.add_job_failure_comment(
                        workflow.jira_ticket_id,
                        workflow.alert_name,
                        reason,
                        workflow.rundeck_execution_url
                    )
                else:
                    await self.jira_client.add_remediation_failure_comment(
                        workflow.jira_ticket_id,
                        workflow.alert_name,
                        reason,
                        workflow.rundeck_execution_url
                    )
            except Exception as e:
                logger.error(f"Failed to update JIRA: {e}")
        
        # Send Slack escalation
        if self.slack_client:
            try:
                jira_url = None
                if self.config.jira and workflow.jira_ticket_id:
                    jira_url = f"{self.config.jira.base_url}/browse/{workflow.jira_ticket_id}"
                
                await self.slack_client.send_escalation(
                    alert_name=workflow.alert_name,
                    alert_labels=workflow.alert_labels,
                    reason=reason,
                    jira_ticket_id=workflow.jira_ticket_id,
                    jira_url=jira_url,
                    rundeck_url=workflow.rundeck_execution_url
                )
            except Exception as e:
                logger.error(f"Failed to send Slack escalation: {e}")
        
        workflow.update_state(RemediationState.ESCALATED)
        await self.state_store.save(workflow)
    
    async def get_workflow_status(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """Get the current status of a workflow."""
        workflow = await self.state_store.get(workflow_id)
        if not workflow:
            return None
        
        return {
            "id": workflow.id,
            "alert_name": workflow.alert_name,
            "state": workflow.state.value,
            "rundeck_execution_id": workflow.rundeck_execution_id,
            "jira_ticket_id": workflow.jira_ticket_id,
            "created_at": workflow.created_at.isoformat(),
            "updated_at": workflow.updated_at.isoformat(),
            "error_message": workflow.error_message
        }
    
    async def list_active_workflows(self) -> list:
        """List all active remediation workflows."""
        workflows = await self.state_store.list_active()
        return [
            {
                "id": w.id,
                "alert_name": w.alert_name,
                "state": w.state.value,
                "created_at": w.created_at.isoformat()
            }
            for w in workflows
        ]
    
    async def shutdown(self) -> None:
        """Cancel all running workflow monitors."""
        for task in self._running_tasks.values():
            task.cancel()
        
        if self._running_tasks:
            await asyncio.gather(*self._running_tasks.values(), return_exceptions=True)
        
        self._running_tasks.clear()
        logger.info("RemediationManager shutdown complete")
