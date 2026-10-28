"""Shared test setup. Page tests written before sign-in existed run with TEAMPULSE_AUTH=off;
tests/test_auth.py and tests/test_roles_ui.py switch it on to test both roles."""
import pytest


@pytest.fixture(autouse=True)
def _auth_off_by_default(monkeypatch):
    monkeypatch.setenv("TEAMPULSE_AUTH", "off")
