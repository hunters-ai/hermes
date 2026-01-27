from fastapi.testclient import TestClient
from main import app
import os
import json

# Ensure we use the sample config
os.environ["CONFIG_PATH"] = "config/config.yaml"

client = TestClient(app)

def test_metrics_endpoint():
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "hermes_incoming_requests_total" in response.text

def test_alert_processing_success():
    payload = {
        "receiver": "test-receiver",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "TestAlert",
                    "severity": "critical"
                },
                "startsAt": "2023-01-01T00:00:00Z"
            }
        ],
        "commonLabels": {
            "alertname": "TestAlert",
            "instance": "localhost:9090",
            "job": "prometheus"
        },
        "externalURL": "http://prometheus:9090",
        "version": "4"
    }
    
    # We expect a 500 or 400 because the Webhook URL (localhost:4440) is not reachable.
    # But checking the logs or the error message will confirm if it TRIED to send.
    # Our code catches the connection error and returns 500.
    
    response = client.post("/api/v1/alerts", json=payload)
    
    # If it reached the webhook sending part, it means it passed validation.
    # The error message should mention connection error to localhost:4440.
    assert response.status_code == 500
    assert "Error sending to webhook" in response.json()["error"]
    print("TestAlert processed successfully (failed at webhook as expected)")

def test_alert_missing_fields():
    payload = {
        "receiver": "test-receiver",
        "alerts": [],
        "commonLabels": {
            "alertname": "TestAlert",
            # Missing "instance" and "job"
        }
    }
    
    response = client.post("/api/v1/alerts", json=payload)
    assert response.status_code == 400
    assert "Missing required fields" in response.json()["error"]
    print("Missing fields validation passed")

def test_unknown_alert():
    payload = {
        "receiver": "test-receiver",
        "alerts": [],
        "commonLabels": {
            "alertname": "UnknownAlert"
        }
    }
    
    # Logic: if no config found, it raises ValueError, caught and returns 500
    response = client.post("/api/v1/alerts", json=payload)
    assert response.status_code == 500
    assert "no configuration found" in response.json()["error"]
    print("Unknown alert validation passed")

if __name__ == "__main__":
    test_metrics_endpoint()
    test_alert_processing_success()
    test_alert_missing_fields()
    test_unknown_alert()
    print("All tests passed!")
