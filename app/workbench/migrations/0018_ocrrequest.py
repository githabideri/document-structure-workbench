import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workbench', '0017_chatthread_archived'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='OcrRequest',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('target', models.CharField(choices=[('region', 'Region'), ('page', 'Page')], default='region', max_length=20)),
                ('provider', models.CharField(default='openai-compatible', max_length=80)),
                ('model', models.CharField(blank=True, max_length=200)),
                ('prompt', models.TextField()),
                ('input_sha256', models.CharField(blank=True, max_length=64)),
                ('input_metadata', models.JSONField(blank=True, default=dict)),
                ('state', models.CharField(choices=[('queued', 'Queued'), ('processing', 'Processing'), ('completed', 'Completed'), ('failed', 'Failed'), ('cancelled', 'Cancelled')], default='queued', max_length=20)),
                ('candidate_text', models.TextField(blank=True)),
                ('raw_response', models.JSONField(blank=True, default=dict)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('error_message', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('accepted_correction', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='ocr_request', to='workbench.regioncorrection')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='ocr_requests', to=settings.AUTH_USER_MODEL)),
                ('document', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ocr_requests', to='workbench.document')),
                ('page', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ocr_requests', to='workbench.page')),
                ('region', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='ocr_requests', to='workbench.pageregion')),
                ('source_document', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ocr_requests', to='workbench.sourcedocument')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
    ]
