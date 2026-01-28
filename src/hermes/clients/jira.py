"""JIRA API client for updating tickets and adding comments."""
import logging
from typing import Optional, Dict, Any
import httpx
from base64 import b64encode

logger = logging.getLogger(__name__)


class JiraClient:
    """Client for interacting with JIRA REST API."""
    
    def __init__(self, base_url: str, user_email: str, api_token: str):
        self.base_url = base_url.rstrip('/')
        self.user_email = user_email
        self.api_token = api_token
    
    def _get_headers(self) -> Dict[str, str]:
        # JIRA Cloud uses Basic Auth with email:api_token
        auth_string = f"{self.user_email}:{self.api_token}"
        auth_bytes = b64encode(auth_string.encode()).decode()
        
        return {
            "Authorization": f"Basic {auth_bytes}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
    
    async def add_comment(
        self, 
        ticket_id: str, 
        comment_text: str,
        visibility: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Add a comment to a JIRA ticket.
        
        Args:
            ticket_id: JIRA ticket ID (e.g., "OPS-1234")
            comment_text: Plain text comment to add
            visibility: Optional visibility restriction
        
        Returns:
            Created comment object
        """
        url = f"{self.base_url}/rest/api/3/issue/{ticket_id}/comment"
        
        # JIRA API v3 uses ADF (Atlassian Document Format)
        body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": comment_text
                            }
                        ]
                    }
                ]
            }
        }
        
        if visibility:
            body["visibility"] = visibility
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=self._get_headers(), json=body)
                response.raise_for_status()
                result = response.json()
                logger.info(f"Added comment to JIRA ticket {ticket_id}")
                return result
            except httpx.HTTPError as e:
                logger.error(f"Error adding comment to JIRA {ticket_id}: {e}")
                if hasattr(e, 'response') and e.response is not None:
                    logger.error(f"Response: {e.response.text}")
                raise
    
    async def add_remediation_success_comment(
        self, 
        ticket_id: str, 
        alert_name: str,
        rundeck_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Add a success comment for auto-remediation."""
        comment = f"✅ Auto-remediation successful for alert: {alert_name}\n"
        comment += "Alert has been resolved after Rundeck job execution.\n"
        if rundeck_url:
            comment += f"Rundeck execution: {rundeck_url}"
        
        return await self.add_comment(ticket_id, comment)
    
    async def add_remediation_failure_comment(
        self, 
        ticket_id: str, 
        alert_name: str,
        reason: str,
        rundeck_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Add a failure comment when remediation didn't resolve the alert."""
        comment = f"❌ Auto-remediation failed for alert: {alert_name}\n"
        comment += f"Reason: {reason}\n"
        if rundeck_url:
            comment += f"Rundeck execution: {rundeck_url}\n"
        comment += "Escalating to NOC on-call for manual intervention."
        
        return await self.add_comment(ticket_id, comment)
    
    async def add_job_failure_comment(
        self, 
        ticket_id: str, 
        alert_name: str,
        error_message: str,
        rundeck_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Add a comment when Rundeck job execution failed."""
        comment = f"⚠️ Rundeck job execution failed for alert: {alert_name}\n"
        comment += f"Error: {error_message}\n"
        if rundeck_url:
            comment += f"Rundeck execution: {rundeck_url}\n"
        comment += "Escalating to NOC on-call for manual intervention."
        
        return await self.add_comment(ticket_id, comment)
    
    async def get_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        """Get ticket details."""
        url = f"{self.base_url}/rest/api/3/issue/{ticket_id}"
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, headers=self._get_headers())
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    logger.warning(f"JIRA ticket {ticket_id} not found")
                    return None
                raise
