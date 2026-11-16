import pytest
from django.test import Client


@pytest.mark.django_db
def test_all_html5_pages_and_health_endpoint():
    client = Client()
    for path in [
        "/",
        "/login/",
        "/register/",
        "/dashboard/",
        "/meeting/avr-test-1234/",
        "/profile/",
        "/admin-panel/",
    ]:
        res = client.get(path)
        assert res.status_code == 200, f"Expected 200 for {path}, got {res.status_code}"
        assert b"CONVIA" in res.content

    health = client.get("/api/v1/health/")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
