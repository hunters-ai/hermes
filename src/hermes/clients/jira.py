"""JIRA API client for updating tickets and adding comments."""
import logging
from typing import Optional, Dict, Any, List
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
    
    async def search_tickets(
        self, 
        jql: str, 
        max_results: int = 1,
        fields: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Search for JIRA tickets using JQL.
        
        Args:
            jql: JQL query string
            max_results: Maximum number of results to return
            fields: List of fields to return (defaults to key, summary)
        
        Returns:
            List of matching tickets
        """
        url = f"{self.base_url}/rest/api/3/search"
        
        payload = {
            "jql": jql,
            "maxResults": max_results,
            "fields": fields or ["key", "summary", "created"]
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=self._get_headers(), json=payload)
                response.raise_for_status()
                result = response.json()
                issues = result.get("issues", [])
                logger.info(f"JQL search returned {len(issues)} tickets (max: {max_results})")
                return issues
            except httpx.HTTPError as e:
                logger.error(f"Error searching JIRA tickets: {e}")
                if hasattr(e, 'response') and e.response is not None:
                    logger.error(f"Response: {e.response.text}")
                raise
    
    async def find_ticket_by_summary(
        self, 
        summary_search_text: str,
        reporter_account_id: str = "712020:b70007c3-9e04-441b-bea0-2c3ef9cdb250",
        hours_ago: int = 30
    ) -> Optional[str]:
        """
        Find a JIRA ticket by summary text search.
        
        Used to find tickets created by jira-alert service for specific alerts.
        
        Args:
            summary_search_text: Text to search for in ticket summary (e.g., dataflow_id)
            reporter_account_id: JIRA account ID of the jira-alert service reporter
            hours_ago: How far back to search (default 1 hour)
        
        Returns:
            Ticket key (e.g., "OPS-1234") if found, None otherwise
        """
        # Build JQL query matching the NOC ticket pattern
        jql = (
            f'"Responsible Team" = "SRE-NOC" '
            f'AND created > -{hours_ago}d '
            f'AND labels in ("NOC", "ingestion-content") '
            f'AND reporter = {reporter_account_id} '
            f'AND summary ~ "{summary_search_text}" '
            f'ORDER BY created DESC'
        )
        
        logger.info(f"Searching JIRA for ticket with summary containing: {summary_search_text}")
        logger.debug(f"JQL query: {jql}")
        
        try:
            tickets = await self.search_tickets(jql, max_results=1)
            if tickets:
                ticket_key = tickets[0].get("key")
                logger.info(f"Found JIRA ticket: {ticket_key}")
                return ticket_key
            else:
                logger.warning(f"No JIRA ticket found for summary search: {summary_search_text}")
                return None
        except Exception as e:
            logger.error(f"Failed to find JIRA ticket for '{summary_search_text}': {e}")
            return None
