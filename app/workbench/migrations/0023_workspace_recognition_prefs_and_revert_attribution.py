"""Per-user recognition preferences + correction revert attribution.

Adds ``UserPreferences.last_htr_pipeline`` / ``last_vision_provider`` so the
document workspace can remember the last-used recognition models (these follow
the user across browsers), and ``RegionCorrection.reverted_by`` so a revert
records who performed it. Both new columns are nullable/blank so existing rows
migrate without data loss.
"""
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("workbench", "0022_project_curator_group"),
    ]

    operations = [
        migrations.AddField(
            model_name="userpreferences",
            name="last_htr_pipeline",
            field=models.CharField(
                blank=True, default="", help_text="Last-used HTR pipeline id for the document workspace.",
                max_length=80,
            ),
        ),
        migrations.AddField(
            model_name="userpreferences",
            name="last_vision_provider",
            field=models.CharField(
                blank=True, default="",
                help_text="Last-used visual OCR provider/model for the document workspace.",
                max_length=200,
            ),
        ),
        migrations.AddField(
            model_name="regioncorrection",
            name="reverted_by",
            field=models.ForeignKey(
                blank=True, help_text="Who reverted this correction. Null for historical rows.",
                null=True, on_delete=models.SET_NULL,
                related_name="reverted_region_corrections",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
