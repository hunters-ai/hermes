"""Alertmanager API client for checking alert status."""
import logging
from typing import Dict, List, Optional, Any
import httpx
from urllib.parse import urlencode

logger = logging.getLogger(__name__)


class AlertmanagerClient:
    """Client for querying Alertmanager API to check if alerts are still firing."""
    
    def __init__(self, base_url: str, bearer_token: Optional[str] = None):
        self.base_url = base_url.rstrip('/')
        self.bearer_token = bearer_token
    
    def _get_headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers
    
    async def get_alerts(self, label_matchers: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """
        Get alerts from Alertmanager, optionally filtered by labels.
        
        Args:
            label_matchers: Dict of label key-value pairs to filter by
        
        Returns:
            List of alert objects from Alertmanager
        """
        url = f"{self.base_url}/api/v2/alerts"
        
        # Alertmanager API v2 uses multiple 'filter' query params
        # Format: ?filter=alertname="value"&filter=severity="critical"
        query_params = []
        if label_matchers:
            for key, value in label_matchers.items():
                # Each filter is a separate query param with format: label="value"
                query_params.append(("filter", f'{key}="{value}"'))
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, headers=self._get_headers(), params=query_params)
                response.raise_for_status()
                alerts = response.json()
                logger.debug(f"Retrieved {len(alerts)} alerts with {len(query_params)} filters")
                return alerts
            except httpx.HTTPError as e:
                logger.error(f"Error fetching alerts from Alertmanager: {e}")
                raise
    
    async def is_alert_firing(
        self, 
        alertname: str, 
        additional_labels: Optional[Dict[str, str]] = None
    ) -> bool:
        """
        Check if a specific alert is currently firing.
        
        Args:
            alertname: The name of the alert to check
            additional_labels: Additional labels to match (e.g., instance, job)
        
        Returns:
            True if alert is still firing, False if resolved
        """
        matchers = {"alertname": alertname}
        if additional_labels:
            matchers.update(additional_labels)
        
        alerts = await self.get_alerts(matchers)
        
        # Filter for firing alerts only (exclude silenced, inhibited)
        firing_alerts = [
            a for a in alerts 
            if a.get("status", {}).get("state") == "active"
        ]
        
        is_firing = len(firing_alerts) > 0
        logger.info(f"Alert '{alertname}' is {'firing' if is_firing else 'not firing'}")
        return is_firing
    
    async def get_alert_details(
        self, 
        alertname: str, 
        additional_labels: Optional[Dict[str, str]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Get details of a specific alert if it's firing.
        
        Returns:
            Alert object if firing, None if not found/resolved
        """
        matchers = {"alertname": alertname}
        if additional_labels:
            matchers.update(additional_labels)
        
        alerts = await self.get_alerts(matchers)
        
        # Return first firing alert
        for alert in alerts:
            if alert.get("status", {}).get("state") == "active":
                return alert
        
        return None
