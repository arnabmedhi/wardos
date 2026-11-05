"""Every ward-record test goes through the sign-in."""
import pytest


@pytest.fixture(autouse=True)
def signed_in_as_dr_a(monkeypatch):
    monkeypatch.setenv("WARDOS_DEV_DOCTOR", "dr_a")
    monkeypatch.delenv("K_SERVICE", raising=False)
