from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("db", "0130_meaningful_activity"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.AddField(model_name="issueview", name="filter_meta", field=models.JSONField(default=dict)),
        migrations.AddField(model_name="issueview", name="revision", field=models.PositiveBigIntegerField(default=1)),
        migrations.CreateModel(
            name="IssueViewCreateReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("request_id", models.CharField(max_length=128)),
                ("payload_hash", models.CharField(max_length=64)),
                ("view_id", models.UUIDField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="db.project")),
            ],
            options={
                "db_table": "issue_view_create_receipts",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("project", "actor", "request_id"), name="view_create_actor_request_unique"
                    )
                ],
            },
        ),
    ]
