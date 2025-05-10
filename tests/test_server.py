import pytest
from fastapi.testclient import TestClient
from src.server import app, Message

client = TestClient(app)

def test_root_endpoint():
    """Test the root endpoint"""
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "running", "service": "core-mcp-server"}

def test_ping_message():
    """Test handling of ping message"""
    message = {
        "type": "ping",
        "payload": {},
        "metadata": None
    }
    response = client.post("/message", json=message)
    assert response.status_code == 200
    assert response.json() == {"status": "pong"}

def test_echo_message():
    """Test handling of echo message"""
    test_data = {"test": "data"}
    message = {
        "type": "echo",
        "payload": test_data,
        "metadata": None
    }
    response = client.post("/message", json=message)
    assert response.status_code == 200
    assert response.json() == {"status": "success", "data": test_data}

def test_invalid_message_type():
    """Test handling of invalid message type"""
    message = {
        "type": "invalid",
        "payload": {},
        "metadata": None
    }
    response = client.post("/message", json=message)
    assert response.status_code == 400
    assert "Unknown message type" in response.json()["detail"]

def test_health_check():
    """Test health check endpoint"""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "running", "service": "kube-core-mcp"}
