"""
Remediation manager - orchestrates the full remediation lifecycle.

This is the core orchestration component that:
1. Tracks triggered Rundeck jobs
2. Polls for job completion
3. Waits configured time after success
4. Checks if alert is resolved via Alertmanager
5. Updates JIRA with results
6. Escalates to Slack if remediation failed
"""
import asyncio
import logging
import uuid
from typing import Dict, Any, Optional, Tuple, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass, field

from hermes.config import Config
from hermes.core.state_store import StateStore, RemediationWorkflow, RemediationState
from hermes.core.job_monitor import JobMonitor
from hermes.clients.alertmanager import AlertmanagerClient
from hermes.clients.rundeck import RundeckClient
from hermes.clients.jira import JiraClient
from hermes.clients.slack import SlackClient


logger = logging.getLogger(__name__)


@dataclass
class CircuitBreakerState:
    """Track circuit breaker state for external services."""
    failures: int = 0
    last_failure: Optional[datetime] = None
    is_open: bool = False
    
    def record_failure(self):
        self.failures += 1
        self.last_failure = datetime.utcnow()
    
    def record_success(self):
        self.failures = 0
        self.is_open = False
    
    def should_allow_request(self, failure_threshold: int = 5, recovery_timeout_seconds: int = 60) -> bool:
        """Check if request should be allowed based on circuit state."""
        if self.failures < failure_threshold:
            return True
        
        if self.last_failure and (datetime.utcnow() - self.last_failure).total_seconds() > recovery_timeout_seconds:
            # Allow one request to test if service recovered
            return True
        
        return False


@dataclass  
class AlertCooldown:
    """Track cooldown state for alert deduplication."""
    last_triggered: datetime
    workflow_id: str


# Metrics callback type for recording outcomes
MetricsCallback = Callable[[str, str], None]  # (alert_name, outcome) -> None


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
    
    # Default circuit breaker settings (can be overridden by config)
    DEFAULT_CIRCUIT_FAILURE_THRESHOLD = 5
    DEFAULT_CIRCUIT_RECOVERY_TIMEOUT = 60  # seconds
    
    # Default concurrent workflow limits (can be overridden by config)
    DEFAULT_MAX_CONCURRENT_WORKFLOWS = 100
    DEFAULT_MAX_WORKFLOWS_PER_ALERT = 3
    
    def __init__(
        self, 
        config: Config, 
        state_store: StateStore, 
        rundeck_client: RundeckClient,
        metrics_callback: Optional[MetricsCallback] = None
    ):
        self.config = config
        self.state_store = state_store
        
        # Get limits from config or use defaults
        self.max_concurrent_workflows = getattr(
            config.remediation, 'max_concurrent_workflows', 
            self.DEFAULT_MAX_CONCURRENT_WORKFLOWS
        )
        self.circuit_failure_threshold = getattr(
            config.remediation, 'circuit_breaker_failure_threshold',
            self.DEFAULT_CIRCUIT_FAILURE_THRESHOLD
        )
        self.circuit_recovery_timeout = getattr(
            config.remediation, 'circuit_breaker_recovery_seconds',
            self.DEFAULT_CIRCUIT_RECOVERY_TIMEOUT
        )
        
        # Metrics callback for recording outcomes
        self._metrics_callback = metrics_callback
        
        # Use shared client
        self.job_monitor = JobMonitor(rundeck_client)
        self.rundeck_client = rundeck_client
        
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
        
        # Circuit breakers for external services
        self._circuit_breakers: Dict[str, CircuitBreakerState] = {
            "rundeck": CircuitBreakerState(),
            "alertmanager": CircuitBreakerState(),
            "jira": CircuitBreakerState(),
            "slack": CircuitBreakerState(),
        }
        
        # Alert cooldown tracking (alert_fingerprint -> cooldown state)
        self._alert_cooldowns: Dict[str, AlertCooldown] = {}
        
        # Rate limiting per Alertmanager source
        self._rate_limiters: Dict[str, Any] = {}
    
    def _get_alert_fingerprint(self, alert_name: str, alert_labels: Dict[str, str]) -> str:
        """Generate a unique fingerprint for an alert based on name and key labels."""
        # Sort labels for consistent fingerprint
        sorted_labels = sorted(alert_labels.items())
        label_str = ",".join(f"{k}={v}" for k, v in sorted_labels)
        return f"{alert_name}:{label_str}"
    
    async def check_deduplication(
        self, 
        alert_name: str, 
        alert_labels: Dict[str, str],
        alert_cooldown_override: Optional[int] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Check if this alert should be deduplicated (skipped).
        
        Args:
            alert_name: Name of the alert
            alert_labels: Alert labels for fingerprinting
            alert_cooldown_override: Per-alert cooldown override (minutes), uses global if None
        
        Returns:
            Tuple of (should_skip, reason, existing_workflow_id)
        """
        fingerprint = self._get_alert_fingerprint(alert_name, alert_labels)
        
        # Check 1: Is there an active workflow for this exact alert?
        existing_workflow = await self.state_store.get_by_alert_labels(alert_name, alert_labels)
        if existing_workflow:
            return (
                True, 
                f"Active workflow {existing_workflow.id} already exists for this alert",
                existing_workflow.id
            )
        
        # Check 2: Is this alert in cooldown period?
        # Use per-alert cooldown if provided, otherwise fall back to global config
        cooldown_config = alert_cooldown_override if alert_cooldown_override is not None else getattr(self.config.remediation, 'job_retrigger_cooldown_minutes', 5)
        if fingerprint in self._alert_cooldowns:
            cooldown = self._alert_cooldowns[fingerprint]
            elapsed = (datetime.utcnow() - cooldown.last_triggered).total_seconds() / 60
            if elapsed < cooldown_config:
                return (
                    True,
                    f"Alert in cooldown ({elapsed:.1f}/{cooldown_config} minutes), last workflow: {cooldown.workflow_id}",
                    cooldown.workflow_id
                )
        
        # Check 3: Have we hit the concurrent workflow limit?
        if len(self._running_tasks) >= self.max_concurrent_workflows:
            return (
                True,
                f"Max concurrent workflows ({self.max_concurrent_workflows}) reached",
                None
            )
        
        return (False, None, None)
    
    def check_circuit_breaker(self, service: str) -> Tuple[bool, str]:
        """
        Check if circuit breaker allows request to service.
        
        Returns:
            Tuple of (is_allowed, reason)
        """
        if service not in self._circuit_breakers:
            return (True, "")
        
        cb = self._circuit_breakers[service]
        if cb.should_allow_request(self.circuit_failure_threshold, self.circuit_recovery_timeout):
            return (True, "")
        
        return (False, f"Circuit breaker OPEN for {service} after {cb.failures} failures")
    
    def record_circuit_success(self, service: str):
        """Record successful call to service."""
        if service in self._circuit_breakers:
            self._circuit_breakers[service].record_success()
    
    def record_circuit_failure(self, service: str):
        """Record failed call to service."""
        if service in self._circuit_breakers:
            self._circuit_breakers[service].record_failure()
            cb = self._circuit_breakers[service]
            if cb.failures >= self.circuit_failure_threshold:
                cb.is_open = True
                logger.warning(f"Circuit breaker OPENED for {service} after {cb.failures} failures")
    
    def _record_outcome(self, alert_name: str, outcome: str):
        """Record remediation outcome metric."""
        if self._metrics_callback:
            try:
                self._metrics_callback(alert_name, outcome)
            except Exception as e:
                logger.warning(f"Failed to record metric: {e}")
    
    async def start_remediation(
        self,
        alert_name: str,
        alert_labels: Dict[str, str],
        rundeck_execution_id: str,
        rundeck_execution_url: Optional[str] = None,
        alertmanager_url: Optional[str] = None,
        rundeck_options: Optional[Dict[str, str]] = None
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
            rundeck_options: Rundeck job options used for the trigger (stored for retries)
        
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
            alertmanager_url=alertmanager_url,
            attempts=1,
            last_triggered_at=datetime.utcnow(),
            rundeck_options=rundeck_options
        )
        
        await self.state_store.save(workflow)
        logger.info(f"Started remediation workflow {workflow_id} for alert {alert_name}")
        
        # Record cooldown for this alert
        fingerprint = self._get_alert_fingerprint(alert_name, alert_labels)
        self._alert_cooldowns[fingerprint] = AlertCooldown(
            last_triggered=datetime.utcnow(),
            workflow_id=workflow_id
        )
        
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
            while True:
                workflow = await self.state_store.get(workflow_id)
                if not workflow:
                    logger.error(f"Workflow {workflow_id} not found")
                    return
                
                # Get alert-specific config
                alert_config = self.config.get_alert_config(workflow.alert_name)
                resolution_wait = self.config.remediation.alertmanager_check_delay_minutes
                if alert_config and alert_config.remediation.alertmanager_check_delay_minutes:
                    resolution_wait = alert_config.remediation.alertmanager_check_delay_minutes
                
                # Check if we should skip resolution check (for alerts requiring customer action)
                skip_resolution_check = alert_config and alert_config.remediation.skip_resolution_check
                
                # Step 1: Wait for job completion
                if workflow.state in [RemediationState.JOB_TRIGGERED, RemediationState.JOB_RUNNING]:
                    job_success = await self._wait_for_job_completion(workflow)
                    
                    if not job_success:
                        # Job failed - escalate immediately
                        await self._handle_job_failure(workflow)
                        return
                
                # Step 2: If skip_resolution_check is set, mark as success without waiting for alert resolution
                if skip_resolution_check:
                    logger.info(f"Job succeeded, skipping resolution check for {workflow.alert_name} (customer action required)")
                    await self._handle_success_no_resolution_check(workflow)
                    return
                
                # Step 3: Wait for alert resolution (with polling and early resolution support)
                logger.info(f"Job succeeded, waiting up to {resolution_wait} minutes for alert resolution")
                workflow.update_state(RemediationState.WAITING_RESOLUTION)
                await self.state_store.save(workflow)
                
                alert_resolved = await self._wait_for_alert_resolution(workflow, resolution_event, resolution_wait)
                
                if alert_resolved:
                    await self._handle_success(workflow)
                    return
                else:
                    retry_initiated = await self._handle_alert_still_firing(workflow)
                    if not retry_initiated:
                        return
                    # Retry was initiated, loop around and monitor the new job
                    # Clear resolution event for the new job
                    resolution_event.clear()
                
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
        alertmanager_check_delay_minutes: int
    ) -> bool:
        """
        Wait for alert resolution via polling or early resolution webhook.
        
        Args:
            workflow: The remediation workflow
            resolution_event: Event that gets set when a resolved webhook arrives
            alertmanager_check_delay_minutes: Minutes to wait before checking Alertmanager
        
        Returns:
            True if alert resolved, False if timeout reached
        """
        check_interval = self.config.remediation.alert_check_interval_seconds
        max_wait = alertmanager_check_delay_minutes * 60
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
        
        # Record success metric
        self._record_outcome(workflow.alert_name, "success")
        
        # Update JIRA
        if self.jira_client and workflow.jira_ticket_id:
            try:
                await self.jira_client.add_remediation_success_comment(
                    workflow.jira_ticket_id,
                    workflow.alert_name,
                    workflow.rundeck_execution_url
                )
                self.record_circuit_success("jira")
            except Exception as e:
                self.record_circuit_failure("jira")
                logger.error(f"Failed to update JIRA: {e}")
        
        workflow.update_state(RemediationState.COMPLETED)
        await self.state_store.save(workflow)
    
    async def _handle_success_no_resolution_check(self, workflow: RemediationWorkflow) -> None:
        """
        Handle successful job execution when skip_resolution_check is enabled.
        
        Used for alerts that require customer action and won't auto-resolve.
        We mark as completed without checking alert status or adding JIRA comments.
        """
        workflow.update_state(RemediationState.COMPLETED)
        await self.state_store.save(workflow)
        
        logger.info(f"Job execution completed for {workflow.alert_name} (resolution check skipped)")
        
        # Record success metric with specific outcome
        self._record_outcome(workflow.alert_name, "job_success_no_resolution_check")
    
    async def _handle_alert_still_firing(self, workflow: RemediationWorkflow) -> bool:
        """
        Called when we checked Alertmanager after job success and the alert is still firing.
        Implements retry policy: retrigger up to max_attempts, respecting cooldown.
        Returns True if a retry was initiated, False if max attempts reached or error occurred.
        """
        logger.info(f"Alert still firing for workflow {workflow.id} (attempts={workflow.attempts})")

        # Determine per-alert max attempts
        alert_cfg = self.config.get_alert_config(workflow.alert_name)
        max_attempts = getattr(self.config.remediation, 'max_attempts', 2)
        if alert_cfg and getattr(alert_cfg.remediation, 'max_attempts', None) is not None:
            max_attempts = alert_cfg.remediation.max_attempts

        # Determine cooldown (minutes) — per-alert override or global
        cooldown_minutes = getattr(self.config.remediation, 'job_retrigger_cooldown_minutes', 5)
        if alert_cfg and getattr(alert_cfg.remediation, 'job_retrigger_cooldown_minutes', None) is not None:
            cooldown_minutes = alert_cfg.remediation.job_retrigger_cooldown_minutes

        # If attempts left, attempt retrigger, else escalate
        if workflow.attempts < max_attempts:
            last = workflow.last_triggered_at or workflow.created_at
            elapsed_min = (datetime.utcnow() - last).total_seconds() / 60.0

            # Respect cooldown: if not enough time elapsed, wait remaining time
            if elapsed_min < cooldown_minutes:
                wait_seconds = int((cooldown_minutes - elapsed_min) * 60.0)
                logger.info(f"Respecting cooldown for workflow {workflow.id}: waiting {wait_seconds}s before retrigger")
                await asyncio.sleep(wait_seconds)

            # Re-trigger Rundeck job using stored options
            options = workflow.rundeck_options
            if options is None:
                # If options are not available, we cannot retrigger reliably — escalate
                logger.error(f"Cannot retrigger workflow {workflow.id} because rundeck_options are missing")
                await self._escalate(workflow, "missing_rundeck_options_for_retrigger")
                return False

            try:
                logger.info(f"Retriggering Rundeck job for workflow {workflow.id} (attempt {workflow.attempts + 1}/{max_attempts})")
                res = await self.rundeck_client.run_job(alert_cfg.job_id, options)
                new_exec_id = str(res.get("id", ""))
                new_exec_url = res.get("permalink") or res.get("permalinkUrl") or res.get("url")

                # Update workflow with new job execution details and increment attempts
                workflow.rundeck_execution_id = new_exec_id
                workflow.rundeck_execution_url = new_exec_url
                workflow.attempts = (workflow.attempts or 0) + 1
                workflow.last_triggered_at = datetime.utcnow()
                workflow.update_state(RemediationState.JOB_TRIGGERED)
                await self.state_store.save(workflow)

                logger.info(f"Retriggered remediation for workflow {workflow.id}, now attempts={workflow.attempts}")
                return True

            except Exception as e:
                logger.exception(f"Failed to retrigger job for workflow {workflow.id}: {e}")
                await self._escalate(workflow, f"retrigger_failed: {e}")
                return False

        # No attempts left: escalate and mark failed
        logger.info(f"Max attempts reached for workflow {workflow.id} (attempts={workflow.attempts}/{max_attempts}), escalating")
        workflow.update_state(RemediationState.ESCALATED, error="max_attempts_reached")
        await self.state_store.save(workflow)
        await self._escalate(workflow, "alert still firing after max remediation attempts")
        return False
    
    async def _handle_job_failure(self, workflow: RemediationWorkflow) -> None:
        """Handle Rundeck job failure."""
        reason = workflow.error_message or "Rundeck job execution failed"
        logger.warning(f"Job failed for {workflow.alert_name}: {reason}")
        
        # Record failure metric
        self._record_outcome(workflow.alert_name, "job_failed")
        
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
    
    async def recover_active_workflows(self) -> int:
        """
        Recover active workflows from state store on startup.
        
        This handles the case where Hermes restarts mid-workflow.
        Workflows are resumed based on their last known state.
        
        Returns:
            Number of workflows recovered
        """
        recovered = 0
        try:
            active_workflows = await self.state_store.list_active()
            
            for workflow in active_workflows:
                # Skip if already being monitored (shouldn't happen on fresh start)
                if workflow.id in self._running_tasks:
                    continue
                
                # Check how old this workflow is - skip if too old
                age_hours = (datetime.utcnow() - workflow.created_at).total_seconds() / 3600
                if age_hours > 24:  # Skip workflows older than 24 hours
                    logger.warning(f"Skipping stale workflow {workflow.id} (age: {age_hours:.1f}h)")
                    workflow.update_state(RemediationState.ESCALATED, "Workflow stale after restart")
                    await self.state_store.save(workflow)
                    continue
                
                # Re-populate cooldown tracking
                fingerprint = self._get_alert_fingerprint(workflow.alert_name, workflow.alert_labels)
                self._alert_cooldowns[fingerprint] = AlertCooldown(
                    last_triggered=workflow.created_at,
                    workflow_id=workflow.id
                )
                
                # Resume monitoring based on state
                if workflow.state in [
                    RemediationState.JOB_TRIGGERED,
                    RemediationState.JOB_RUNNING,
                    RemediationState.JOB_SUCCEEDED,
                    RemediationState.WAITING_RESOLUTION
                ]:
                    logger.info(f"Recovering workflow {workflow.id} in state {workflow.state.value}")
                    
                    # Create resolution event
                    resolution_event = asyncio.Event()
                    self._resolution_events[workflow.id] = resolution_event
                    
                    # Start monitoring task
                    task = asyncio.create_task(self._resume_workflow(workflow))
                    self._running_tasks[workflow.id] = task
                    recovered += 1
                else:
                    logger.info(f"Workflow {workflow.id} in terminal state {workflow.state.value}, skipping recovery")
            
            return recovered
            
        except Exception as e:
            logger.error(f"Error recovering workflows: {e}")
            return recovered
    
    async def _resume_workflow(self, workflow: RemediationWorkflow) -> None:
        """
        Resume monitoring a recovered workflow based on its current state.
        """
        resolution_event = self._resolution_events.get(workflow.id)
        if not resolution_event:
            resolution_event = asyncio.Event()
            self._resolution_events[workflow.id] = resolution_event
            
        try:
            # Resume monitor as loop by directly turning this into a while True loop or calling the _monitor_workflow method
            # Actually, _monitor_workflow method is already a while True loop!
            # We can just jump directly into the _monitor_workflow method
            await self._monitor_workflow(workflow.id)
        except asyncio.CancelledError:
            logger.info(f"Recovered workflow {workflow.id} monitoring cancelled")
        except Exception as e:
            logger.exception(f"Error in recovered workflow {workflow.id}: {e}")
            workflow.update_state(RemediationState.ESCALATED, str(e))
            await self.state_store.save(workflow)
        finally:
            self._running_tasks.pop(workflow.id, None)
            self._resolution_events.pop(workflow.id, None)
    
    async def shutdown(self) -> None:
        """Cancel all running workflow monitors."""
        for task in self._running_tasks.values():
            task.cancel()
        
        if self._running_tasks:
            await asyncio.gather(*self._running_tasks.values(), return_exceptions=True)
        
        self._running_tasks.clear()
        logger.info("RemediationManager shutdown complete")
