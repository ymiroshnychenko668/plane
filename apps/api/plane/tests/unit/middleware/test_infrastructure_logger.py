"""Rejected inventory metadata must not escape into ordinary request logs."""

from unittest.mock import Mock

import pytest
from django.http import HttpResponse
from django.test import RequestFactory

from plane.middleware.logger import APITokenLogMiddleware, RequestLoggerMiddleware

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("status", [201, 400, 403, 412])
def test_infrastructure_request_response_and_query_are_redacted(monkeypatch, status):
    process = Mock()
    monkeypatch.setattr("plane.middleware.logger.process_logs.delay", process)
    request = RequestFactory().post(
        "/api/v1/workspaces/example/projects/00000000-0000-0000-0000-000000000001/"
        "infrastructure/resources/?search=password%3Dsynthetic-rejected-value",
        data='{"password":"synthetic-rejected-value"}',
        content_type="application/json",
        HTTP_X_API_KEY="synthetic-api-key",
    )
    response = HttpResponse(b'{"name":"synthetic-rejected-value"}', status=status)
    APITokenLogMiddleware(lambda _request: response)(request)
    stored = process.call_args.kwargs["log_data"]
    assert stored["body"] == stored["response_body"] == "[REDACTED infrastructure metadata]"
    assert stored["query_params"] == "[REDACTED]"
    assert "synthetic-rejected-value" not in repr(stored)
    assert "synthetic-api-key" not in repr(stored)


def test_session_inventory_query_does_not_enter_request_logger(monkeypatch):
    logger = Mock()
    monkeypatch.setattr("plane.middleware.logger.api_logger", logger)
    request = RequestFactory().get(
        "/api/workspaces/example/projects/example/infrastructure/resources/",
        {
            "search": "password=synthetic-rejected-value",
        },
    )
    request.user = Mock(is_authenticated=False)
    response = HttpResponse(status=400)
    RequestLoggerMiddleware(lambda _request: response)(request)
    assert logger.info.called
    assert "synthetic-rejected-value" not in repr(logger.info.call_args)


@pytest.mark.django_db
def test_debug_view_dispatch_does_not_print_rejected_query(api_key_client, workspace, settings, monkeypatch, capsys):
    settings.DEBUG = True
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    monkeypatch.setattr("celery.app.task.Task.delay", Mock())
    response = api_key_client.get(
        f"/api/v1/workspaces/{workspace.slug}/projects/00000000-0000-0000-0000-000000000001/"
        "infrastructure/resources/?search=password%3Dsynthetic-rejected-value"
    )
    assert response.status_code == 403
    captured = capsys.readouterr()
    assert "synthetic-rejected-value" not in captured.out + captured.err
