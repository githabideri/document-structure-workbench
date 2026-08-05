from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("workbench", "0016_evidenceitem_page_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatthread",
            name="is_archived",
            field=models.BooleanField(default=False),
        ),
    ]
