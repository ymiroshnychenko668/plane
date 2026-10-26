from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("db", "0123_project_agent_registration")]

    operations = [
        migrations.AddField(
            model_name="projectagentregistration",
            name="user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="project_agent_registrations",
                to="db.user",
            ),
        ),
        migrations.AddField(
            model_name="projectagentregistration",
            name="api_token",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="project_agent_registrations",
                to="db.apitoken",
            ),
        ),
        migrations.AddField(
            model_name="projectagentregistration", name="provisioning", field=models.JSONField(default=dict)
        ),
    ]
