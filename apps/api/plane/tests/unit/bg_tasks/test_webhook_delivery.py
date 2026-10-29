"""Temporary HTTP errors must activate the existing Celery retry policy."""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
import requests

from plane.bgtasks import webhook_task


@pytest.fixture
def delivery(monkeypatch):
    webhook = SimpleNamespace(
        id=uuid4(), workspace_id=uuid4(), secret_key="test-signing-key", url="https://dispatcher.test/hook"
    )
    monkeypatch.setattr(webhook_task.Webhook.objects, "get", Mock(return_value=webhook))
    log = Mock()
    monkeypatch.setattr(webhook_task, "save_webhook_log", log)
    task = SimpleNamespace(request=SimpleNamespace(retries=0), max_retries=5)
    # Celery wraps .run for autoretry. Call its unwrapped implementation so
    # a network exception is observed without contacting any message broker.
    run = webhook_task.webhook_send_task._orig_run.__func__
    return task, webhook, log, run


@pytest.mark.unit
@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_temporary_http_failure_raises_retryable_exception(delivery, monkeypatch, status):
    task, webhook, log, run = delivery
    response = requests.Response()
    response.status_code = status
    response._content = b"temporary failure"
    response.url = webhook.url
    monkeypatch.setattr(webhook_task, "pinned_fetch", Mock(return_value=response))
    with pytest.raises(requests.RequestException):
        run(task, str(webhook.id), "workspace", "issue", {"id": str(uuid4())}, "created", "", None)
    assert log.call_args.kwargs["response_status"] == status
    assert log.call_args.kwargs["response_body"] == "temporary failure"


@pytest.mark.unit
@pytest.mark.parametrize("status", [200, 202, 400, 401, 404])
def test_success_or_permanent_http_failure_is_logged_without_retry(delivery, monkeypatch, status):
    task, webhook, log, run = delivery
    response = requests.Response()
    response.status_code = status
    response._content = b"receiver response"
    response.url = webhook.url
    monkeypatch.setattr(webhook_task, "pinned_fetch", Mock(return_value=response))
    run(task, str(webhook.id), "workspace", "issue", {}, "created", "", None)
    assert log.call_args.kwargs["response_status"] == status
