from django.db import models

from .project import ProjectBaseModel


class Build(ProjectBaseModel):
    """An observed CI run attempt, independent of its module results."""

    repository = models.CharField(max_length=255)
    workflow = models.CharField(max_length=255)
    run_id = models.CharField(max_length=128)
    attempt = models.PositiveIntegerField()
    revision = models.CharField(max_length=64)
    ref = models.CharField(max_length=512)
    pipeline_url = models.URLField(max_length=2048)
    status = models.CharField(max_length=16, choices=[(s, s) for s in ("queued", "running", "completed", "unknown")])
    conclusion = models.CharField(max_length=64, null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    observed_at = models.DateTimeField()
    freshness_hours = models.FloatField(null=True, blank=True)
    links = models.JSONField(default=list)
    version = models.PositiveBigIntegerField(default=1)

    class Meta:
        db_table = "builds"
        ordering = ("-observed_at", "-created_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=["project", "repository", "run_id", "attempt"],
                condition=models.Q(deleted_at__isnull=True),
                name="build_unique_run_attempt",
            ),
            models.CheckConstraint(condition=models.Q(attempt__gte=1), name="build_positive_attempt"),
        ]
        indexes = [
            models.Index(fields=["project", "repository", "ref", "-observed_at"], name="build_project_ref_observed")
        ]


class BuildResult(ProjectBaseModel):
    """A single module/suite measurement; null module means an explicit aggregate."""

    build = models.ForeignKey(Build, on_delete=models.CASCADE, related_name="results")
    module = models.ForeignKey(
        "db.Module", on_delete=models.CASCADE, related_name="build_results", null=True, blank=True
    )
    suite = models.CharField(max_length=255)
    test_type = models.CharField(max_length=16, choices=[(s, s) for s in ("unit", "integration", "e2e", "other")])
    scope = models.TextField()
    exclusions = models.JSONField(default=list)
    tests = models.JSONField(null=True, blank=True)
    coverage = models.JSONField(default=dict)

    class Meta:
        db_table = "build_results"
        ordering = ("suite", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["build", "module", "suite"],
                condition=models.Q(deleted_at__isnull=True, module__isnull=False),
                name="build_result_unique_module_suite",
            ),
            models.UniqueConstraint(
                fields=["build", "suite"],
                condition=models.Q(deleted_at__isnull=True, module__isnull=True),
                name="build_result_unique_aggregate_suite",
            ),
        ]
