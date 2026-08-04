from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("workbench", "0014_workerheartbeat")]
    operations = [
        migrations.AlterField(
            model_name="chatthread", name="project",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="chat_threads", to="workbench.collection"),
        ),
        migrations.AddField(model_name="chatthread", name="scope_mode", field=models.CharField(choices=[("project", "Project"), ("all", "All accessible projects")], default="project", max_length=20)),
        migrations.AddField(model_name="chatthread", name="scope_config", field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(model_name="chatrun", name="scope_snapshot", field=models.JSONField(blank=True, default=dict)),
    ]
