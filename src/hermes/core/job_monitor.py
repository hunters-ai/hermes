"""Rundeck job monitoring client."""
import logging
from typing import Optional, Dict, Any, Tuple

from hermes.clients.rundeck import RundeckClient

logger = logging.getLogger(__name__)


class JobMonitor:
    """Monitors Rundeck job executions and retrieves execution details."""
    
    def __init__(self, rundeck_client: RundeckClient):
        """
        Initialize JobMonitor with a RundeckClient instance.
        
        Args:
            rundeck_client: Configured RundeckClient (supports token or session auth)
        """
        self.client = rundeck_client
    
    async def get_execution_status(self, execution_id: str) -> Tuple[str, Dict[str, Any]]:
        """
        Get the status of a Rundeck execution.
        
        Returns:
            Tuple of (status, full_response)
            status: "running" | "succeeded" | "failed" | "aborted"
        """
        data = await self.client.get_execution(execution_id)
        status = data.get("status", "unknown")
        return status, data
    
    async def get_execution_options(self, execution_id: str) -> Dict[str, str]:
        """
        Get the options (parameters) that were passed to a Rundeck execution.
        
        This is how we retrieve the JIRA ticket ID that was passed to the job.
        """
        _, data = await self.get_execution_status(execution_id)
        
        # Options are in job.options or argstring
        job_data = data.get("job", {})
        options = job_data.get("options", {})
        
        # Also try parsing from argstring if options is empty
        if not options and "argstring" in data:
            argstring = data.get("argstring", "")
            # Parse -option value format
            options = self._parse_argstring(argstring)
        
        return options
    
    def _parse_argstring(self, argstring: str) -> Dict[str, str]:
        """Parse Rundeck argstring format: -option1 value1 -option2 value2"""
        options = {}
        parts = argstring.split()
        i = 0
        while i < len(parts):
            if parts[i].startswith('-'):
                key = parts[i].lstrip('-')
                if i + 1 < len(parts) and not parts[i + 1].startswith('-'):
                    options[key] = parts[i + 1]
                    i += 2
                else:
                    options[key] = ""
                    i += 1
            else:
                i += 1
        return options
    
    async def get_execution_permalink(self, execution_id: str) -> Optional[str]:
        """Get the permalink URL for a Rundeck execution."""
        _, data = await self.get_execution_status(execution_id)
        return data.get("permalink") or data.get("href")
    
    async def is_job_complete(self, execution_id: str) -> Tuple[bool, Optional[str]]:
        """
        Check if a job has completed.
        
        Returns:
            Tuple of (is_complete, final_status)
            final_status is None if still running
        """
        status, _ = await self.get_execution_status(execution_id)
        
        if status in ("succeeded", "failed", "aborted"):
            return True, status
        return False, None
