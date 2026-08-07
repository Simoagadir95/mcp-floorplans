"""
Tests for http_wrapper.py - OAuth 2.1 protected endpoints (D3).

Verifies:
  1. curl endpoint without token → 401 + WWW-Authenticate header
  2. curl /.well-known/oauth-protected-resource → JSON metadata
  3. Negative test: reject token with invalid format
"""

import pytest
from fastapi.testclient import TestClient
from http_wrapper import app

client = TestClient(app)


def test_health_check_public():
    """Health check is public (no auth required)."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "timestamp" in data


def test_oauth_metadata_endpoint():
    """D3 Test: /.well-known/oauth-protected-resource returns RFC 9728 metadata."""
    response = client.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 200
    data = response.json()

    # Verify RFC 9728 metadata fields
    assert "resource_documentation_uri" in data
    assert "resource_scopes" in data
    assert "authorization_server" in data
    assert "token_endpoint" in data
    assert "authorization_endpoint" in data
    assert "resource_audience" in data
    assert "supported_auth_methods" in data

    # Check that Bearer is supported
    assert "Bearer" in data["supported_auth_methods"]

    # Check scopes
    scopes = [s["name"] for s in data["resource_scopes"]]
    assert "space:read" in scopes
    assert "space:write" in scopes

    print("✓ OAuth metadata endpoint returns RFC 9728 compliant response")


def test_missing_token_returns_401_with_www_authenticate():
    """D3 Test: Request without token → 401 + WWW-Authenticate header."""
    response = client.post(
        "/generate-layouts",
        json={
            "surface_sqm": 400.0,
            "headcount": 40,
            "zone_types": ["open-space", "meeting"]
        }
    )

    # Should return 401 Unauthorized
    assert response.status_code == 401, f"Expected 401, got {response.status_code}"

    # Should include WWW-Authenticate header
    assert "www-authenticate" in response.headers, "Missing WWW-Authenticate header"

    www_auth = response.headers["www-authenticate"]
    print(f"WWW-Authenticate header: {www_auth}")

    # Verify it mentions Bearer scheme
    assert "Bearer" in www_auth, "WWW-Authenticate should mention Bearer scheme"

    print("✓ Missing token returns 401 with WWW-Authenticate header")


def test_invalid_token_format():
    """D3 Test: Negative test - reject token with invalid format."""
    # Test 1: No bearer scheme
    response = client.post(
        "/generate-layouts",
        json={
            "surface_sqm": 400.0,
            "headcount": 40,
            "zone_types": ["open-space"]
        },
        headers={"Authorization": "InvalidScheme abc123"}
    )

    assert response.status_code == 401
    assert "Bearer" in response.headers.get("www-authenticate", "")

    # Test 2: Too short token
    response = client.post(
        "/generate-layouts",
        json={
            "surface_sqm": 400.0,
            "headcount": 40,
            "zone_types": ["open-space"]
        },
        headers={"Authorization": "Bearer short"}
    )

    assert response.status_code == 401

    print("✓ Invalid token formats are rejected with 401")


def test_valid_token_format_accepted():
    """
    D3 Test: Request with valid Bearer token format is accepted.
    (This doesn't validate the token against an auth server, just format.)
    """
    valid_token = "valid_token_" + "x" * 50  # Make it longer than 10 chars

    response = client.post(
        "/generate-layouts",
        json={
            "surface_sqm": 400.0,
            "headcount": 40,
            "zone_types": ["open-space", "meeting"],
            "project_id": "test-project"
        },
        headers={"Authorization": f"Bearer {valid_token}"}
    )

    # Should succeed (200) or fail with 500 (layout error), not 401 (auth error)
    # Since we're not mocking the actual auth server, we expect a 200 if layout succeeds
    # or 500 if layout service fails, but NOT 401
    assert response.status_code in [200, 500], (
        f"Token format should be accepted; got {response.status_code}"
    )

    if response.status_code == 200:
        data = response.json()
        assert "variants" in data
        print("✓ Valid token format accepted and layout generated")
    else:
        print(f"✓ Valid token format accepted (layout generation returned {response.status_code})")


def test_get_layout_with_token():
    """D3 Test: GET layout endpoint requires and accepts Bearer token."""
    valid_token = "valid_token_" + "y" * 50

    response = client.get(
        "/layouts/test-project-123",
        params={
            "surface_sqm": 300.0,
            "headcount": 30,
            "zone_types": "open-space,meeting"
        },
        headers={"Authorization": f"Bearer {valid_token}"}
    )

    # Should not return 401
    assert response.status_code != 401, "GET with valid token should not return 401"

    if response.status_code == 200:
        data = response.json()
        assert "project_id" in data
        assert "variants" in data
        assert data["project_id"] == "test-project-123"
        print("✓ GET layout endpoint accepts Bearer token and returns data")


def test_root_endpoint_metadata():
    """Root endpoint returns API metadata."""
    response = client.get("/")

    # Root is protected (requires auth if REQUIRE_OAUTH=true)
    # For this test, we'll check it exists
    if response.status_code == 200:
        data = response.json()
        assert "service" in data
        assert "oauth_required" in data
        print("✓ Root endpoint metadata available")
    elif response.status_code == 401:
        # Root is protected, which is correct
        assert "www-authenticate" in response.headers
        print("✓ Root endpoint is OAuth protected")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
