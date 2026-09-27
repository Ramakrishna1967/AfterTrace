"""Security + budgets (spec p25)."""

from aftertrace import security as sec


def test_redact_never_persists_secrets():
    s = "call with api-key: SECRET123 and Authorization: Bearer abc.def.ghi"
    r = sec.redact(s)
    assert "SECRET123" not in r and "abc.def.ghi" not in r
    assert "[redacted]" in r


def test_scan_detects_private_key():
    assert sec.scan_for_secrets("-----BEGIN PRIVATE KEY----- xyz")


def test_metadata_allowlist():
    m = sec.sanitize_metadata({"incident_id": "i1", "api-key": "x", "token": "y", "route": "r"})
    assert "incident_id" in m and "route" in m
    assert "api-key" not in m and "token" not in m


def test_scope_binding():
    import pytest

    with pytest.raises(PermissionError):
        sec.require_scope({"project_id": "a"}, "b", "c", "staging")


def test_budget_exhaustion():
    import pytest

    b = sec.Budget(max_dispatches=1, wall_clock_s=60)
    b.check_dispatch()
    with pytest.raises(RuntimeError):
        b.check_dispatch()


def test_truncate_bounds_response():
    b = sec.Budget(response_limit_bytes=10)
    assert b.truncate("x" * 100).endswith("…[truncated]")
