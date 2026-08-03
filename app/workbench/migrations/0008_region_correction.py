from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("workbench", "0007_processing_revisions_and_recovery")]

    operations = [
        migrations.CreateModel(
            name="RegionCorrection",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("operation", models.CharField(choices=[("text", "Correct text"), ("type", "Change region type"), ("suppress", "Suppress region"), ("note", "Add curator note")], max_length=20)),
                ("before", models.JSONField(default=dict)),
                ("after", models.JSONField(default=dict)),
                ("reason", models.TextField(blank=True)),
                ("status", models.CharField(choices=[("active", "Active"), ("reverted", "Reverted")], default="active", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("reverted_at", models.DateTimeField(blank=True, null=True)),
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="region_corrections", to=settings.AUTH_USER_MODEL)),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="region_corrections", to="workbench.document")),
                ("region", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="corrections", to="workbench.pageregion")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
    ]
