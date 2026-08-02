"""Add processed_document FK to SourceDocument."""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("workbench", "0004_seed_processing_presets"),
    ]

    operations = [
        migrations.AddField(
            model_name="sourcedocument",
            name="processed_document",
            field=models.ForeignKey(
                blank=True,
                help_text="Canonical processed Document for this source.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="source_documents",
                to="workbench.document",
            ),
        ),
    ]
