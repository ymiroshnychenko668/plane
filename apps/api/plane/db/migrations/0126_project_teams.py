import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("db", "0125_builds")]
    operations = [
        migrations.CreateModel(
            name="ProjectTeamContact",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("address", models.CharField(max_length=128)),
                ("target", models.JSONField(default=dict)),
                ("active", models.BooleanField(default=True)),
                ("revision", models.PositiveBigIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="team_contacts", to="db.project")),
                ("registration", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="team_contacts", to="db.projectagentregistration")),
            ],
            options={"db_table": "project_team_contacts", "ordering": ("address",), "constraints": [models.UniqueConstraint(fields=("project", "address"), name="project_team_contact_unique_address")]},
        ),
        migrations.CreateModel(
            name="ProjectTeamMigration",
            fields=[
                ("project", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name="team_migration", serialize=False, to="db.project")),
                ("page_id", models.UUIDField(unique=True)),
                ("source_revision", models.CharField(max_length=64)),
                ("imported_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"db_table": "project_team_migrations"},
        ),
    ]
