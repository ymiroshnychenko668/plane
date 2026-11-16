"""Transaction-local provenance for authenticated task text writers.

Database triggers also capture writers outside this boundary, but their event
time is deliberately unknown. Never trust a caller-supplied actor or timestamp.
"""

import uuid
from copy import deepcopy

from django.db import connection, transaction


class ActivityWriterMixin:
    def dispatch(self, request, *args, **kwargs):
        self._activity_writer = request.method not in {"GET", "HEAD", "OPTIONS"} and getattr(
            self, "capture_task_text", False
        )
        if not self._activity_writer:
            return super().dispatch(request, *args, **kwargs)
        with transaction.atomic():
            response = super().dispatch(request, *args, **kwargs)
            # DRF converts exceptions to responses; a failed logical write must
            # roll back all source revisions too, including late import fields.
            if response.status_code >= 400:
                transaction.set_rollback(True)
            return response

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not getattr(self, "_activity_writer", False) or not request.user.is_authenticated:
            return
        data = request.data
        imported = isinstance(data, dict) and (
            any(key in data for key in ("created_at", "created_by", "actor", "external_source", "external_id"))
            or data.get("skip_activity") in (True, "true", "1", 1)
        )
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('plane.activity_writer', %s, true), "
                    "set_config('plane.activity_producer', %s, true), "
                    "set_config('plane.activity_mutation', %s, true)",
                    [str(request.user.pk), "import" if imported else "native", str(uuid.uuid4())],
                )


def task_after_commit(task, *args, **kwargs):
    """Legacy projections may run only after source and ledger become visible."""
    if not connection.in_atomic_block:
        return task.delay(*args, **kwargs)
    frozen_args, frozen_kwargs = deepcopy(args), deepcopy(kwargs)

    def publish():
        task.delay(*frozen_args, **frozen_kwargs)

    transaction.on_commit(publish, robust=True)
