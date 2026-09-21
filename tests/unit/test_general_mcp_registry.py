import pytest

from dududa.core.capability import CapabilityRegistry
from dududa.mcp.registry import (
    MCPProvider,
    ServerCircuitBreaker,
    breaker,
    create_all_services,
    register_all_mcp_services,
    reset_process_state,
)


EXPECTED_SERVICES = {"clock", "web_search", "weather", "news", "translate"}


def test_registry_contains_only_general_purpose_services():
    reset_process_state()
    assert set(create_all_services()) == EXPECTED_SERVICES


def test_capability_registration_contains_only_general_tools():
    reset_process_state()
    registry = CapabilityRegistry()
    assert register_all_mcp_services(registry) == len(EXPECTED_SERVICES)
    ids = {item.capability.capability_id
           for item in registry.filter_candidates(permissions=(), max_count=24)}
    assert ids == {f"mcp.{name}" for name in EXPECTED_SERVICES}


def test_removed_school_capabilities_are_absent():
    reset_process_state()
    registry = CapabilityRegistry()
    register_all_mcp_services(registry)
    for capability_id in (
        "mcp.course_schedule", "mcp.icourse_reviews", "mcp.exam_schedule",
        "mcp.academic_calendar", "mcp.training_program",
        "mcp.second_classroom", "mcp.campus_notice", "mcp.academic_affairs",
    ):
        assert registry.get(capability_id) is None


def test_breaker_opens_and_reset_restores_closed_state(monkeypatch):
    monkeypatch.setattr("dududa.mcp.registry.time.time", lambda: 10.0)
    local = ServerCircuitBreaker(threshold=2, reset_seconds=30)
    local.record_failure("weather")
    assert local.state("weather") == "closed"
    local.record_failure("weather")
    assert local.state("weather") == "open"
    local.reset("weather")
    assert local.state("weather") == "closed"


def test_process_reset_clears_service_and_breaker_state():
    reset_process_state()
    first = create_all_services()
    breaker.record_failure("weather")
    reset_process_state()
    second = create_all_services()
    assert first is second
    assert breaker.state("weather") == "closed"
    assert set(second) == EXPECTED_SERVICES
