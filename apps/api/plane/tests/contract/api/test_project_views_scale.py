"""Large fixture marked slow: run this file explicitly in the disposable PG harness.

No external systems or source snapshots are used. The fixture writes synthetic
rows directly and temporarily disables only the task capture trigger in its own
test database; this avoids measuring fixture capture instead of View evaluation.
"""

import json
from datetime import timedelta
from time import perf_counter
from types import SimpleNamespace

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from plane.db.models import Issue, State
from plane.services.views import ProjectViews
from plane.tests.contract.api.test_project_views import local_dependencies as local_dependencies
from plane.tests.contract.api.test_project_views import project as project
from plane.utils.meaningful_activity import POLICY, SOURCE_SCHEMA, annotate_activity

pytestmark = [pytest.mark.contract, pytest.mark.slow, pytest.mark.django_db(transaction=True)]


def test_large_project_view_bound_queries_and_indexed_activity(create_user, workspace, project, capsys):
    moment = timezone.now()
    old = moment - timedelta(days=30)
    state = State.objects.create(workspace=workspace, project=project, name="Active", group="started")
    fixture_started = perf_counter()
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE issues DISABLE TRIGGER plane_activity_issue_capture")
    try:
        Issue.objects.bulk_create(
            [
                Issue(
                    project=project,
                    workspace=workspace,
                    state=state,
                    name=f"Synthetic item {number:05}",
                    sequence_id=number,
                )
                for number in range(1, 10001)
            ],
            batch_size=1000,
        )
    finally:
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE issues ENABLE TRIGGER plane_activity_issue_capture")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO activity_coverage "
            "(issue_id,captured_from,known_existing_at,native_created_at,generation,source_schema_version) "
            "SELECT id,%s,%s,%s,10,%s FROM issues WHERE project_id=%s",
            [old, old, old, SOURCE_SCHEMA, project.id],
        )
        cursor.execute(
            "INSERT INTO activity_source_events "
            "(id,issue_id,project_id,source_kind,source_id,source_revision,capture_transaction,mutation_id,"
            "source_content,before_content,occurred_at,observed_at,time_quality,is_baseline,"
            "redacted,source_schema_version) "
            "SELECT md5(i.id::text || ':' || s)::uuid,i.id,i.project_id,'description',i.id,s,s,'',"
            "'{\"description_html\":\"Synthetic text, not read by the evaluator.\"}'::jsonb,'{}'::jsonb,"
            "CASE WHEN s=10 AND mod(i.sequence_id,3) IN (1,2) THEN %s ELSE %s END,"
            "CASE WHEN s=10 AND mod(i.sequence_id,3) IN (1,2) THEN %s ELSE %s END,"
            "'native',false,false,%s FROM issues i CROSS JOIN generate_series(1,10) s WHERE i.project_id=%s",
            [moment - timedelta(days=1), old, moment - timedelta(days=1), old, SOURCE_SCHEMA, project.id],
        )
        cursor.execute(
            "INSERT INTO activity_assessments "
            "(id,event_id,policy_version,decision,category,rationale,assessed_at,revision) "
            "SELECT md5(e.id::text || ':assessment')::uuid,e.id,%s,"
            "CASE WHEN e.source_revision=10 AND mod(i.sequence_id,3)=1 THEN 'meaningful' ELSE 'routine' END,"
            "CASE WHEN e.source_revision=10 AND mod(i.sequence_id,3)=1 THEN 'investigation' ELSE 'routine' END,"
            "'Synthetic scale fixture',%s,1 FROM activity_source_events e JOIN issues i ON i.id=e.issue_id "
            "WHERE e.project_id=%s AND NOT(e.source_revision=10 AND mod(i.sequence_id,3)=2)",
            [POLICY, moment, project.id],
        )
        for table in ("issues", "activity_coverage", "activity_source_events", "activity_assessments"):
            cursor.execute(f"ANALYZE {table}")
    fixture_elapsed = perf_counter() - fixture_started
    definition = {
        "filter_meta": {"version": 1, "policy_version": POLICY, "source_schema_version": SOURCE_SCHEMA},
        "rich_filters": {"and": [{"meaningful_activity__older_than_days": 7}]},
        "display_filters": {"layout": "list", "order_by": "id"},
    }
    context = ProjectViews(SimpleNamespace(user=create_user), workspace.slug, project.id)
    samples = []
    for size in (1, 100):
        start = perf_counter()
        with CaptureQueriesContext(connection) as queries:
            result = context.evaluate(definition, {"per_page": size})
        samples.append({"rows": len(result["results"]), "queries": len(queries), "seconds": perf_counter() - start})
        assert len(result["results"]) == size and result["next_page_results"]
        assert result["counts"] == {"matches": 3333, "needs_review": 3333}
        assert len(queries) <= 10
        assert all("source_content" not in row["sql"] and "before_content" not in row["sql"] for row in queries)
    assert samples[0]["queries"] == samples[1]["queries"], samples
    queryset = (
        annotate_activity(
            Issue.all_objects.filter(project=project, deleted_at__isnull=True),
            evaluated_at=moment,
            days=7,
            policy_version=POLICY,
            source_schema_version=SOURCE_SCHEMA,
        )
        .filter(activity_match=True)
        .order_by("id")
        .values("id")[:100]
    )
    plan = json.loads(queryset.explain(format="json", analyze=True, buffers=True))
    indexes = set()

    def inspect(node):
        if "Index Name" in node:
            indexes.add(node["Index Name"])
        for child in node.get("Plans", []):
            inspect(child)

    inspect(plan[0]["Plan"])
    # PostgreSQL may prefer the narrower FK index to the composite index when
    # every task has ten events. Both plans perform bounded per-task lookups.
    assert any(index.startswith(("activity_event_issue_time", "activity_source_events_issue_id")) for index in indexes)
    assert any(index.startswith(("activity_event_policy", "activity_assessments_event_id")) for index in indexes)
    report = {
        "issues": 10000,
        "events": 100000,
        "fixture_seconds": fixture_elapsed,
        "samples": samples,
        "explain_execution_ms": plan[0]["Execution Time"],
        "indexes": sorted(indexes),
    }
    with capsys.disabled():
        print("VIEW_SCALE " + json.dumps(report, sort_keys=True), flush=True)
