"""Add text field to PageRegion for retaining region text content."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workbench", "0005_source_document_processed_document"),
    ]

    operations = [
        migrations.AddField(
            model_name="pageregion",
            name="text",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Region text content.",
            ),
        ),
    ]
