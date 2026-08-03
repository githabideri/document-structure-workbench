import django.db.models.deletion
from django.db import migrations, models


def backfill_revision_links(apps, schema_editor):
    """Attach existing active documents to their latest successful job and regions."""
    SourceDocument = apps.get_model("workbench", "SourceDocument")
    ProcessingJob = apps.get_model("workbench", "ProcessingJob")
    Page = apps.get_model("workbench", "Page")
    PageRegion = apps.get_model("workbench", "PageRegion")

    for source in SourceDocument.objects.exclude(active_document_id=None).iterator():
        job = (
            ProcessingJob.objects.filter(
                source_document_id=source.pk,
                state__in=["completed", "partial"],
                result_document_id=None,
            )
            .order_by("-finished_at", "-created_at")
            .first()
        )
        if job and not ProcessingJob.objects.filter(
            result_document_id=source.active_document_id,
        ).exists():
            job.result_document_id = source.active_document_id
            job.save(update_fields=["result_document"])

    for region in PageRegion.objects.filter(page_id=None).select_related("job").iterator():
        result_document_id = region.job.result_document_id
        if not result_document_id:
            continue
        page = Page.objects.filter(
            document_id=result_document_id,
            page_number=region.page_number,
        ).first()
        if page:
            region.page_id = page.pk
            region.save(update_fields=["page"])


class Migration(migrations.Migration):
    dependencies = [
        ("workbench", "0006_pageregion_text"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="document",
            unique_together=set(),
        ),
        migrations.RenameField(
            model_name="sourcedocument",
            old_name="processed_document",
            new_name="active_document",
        ),
        migrations.AlterField(
            model_name="sourcedocument",
            name="active_document",
            field=models.ForeignKey(
                blank=True,
                help_text="Currently selected completed processing revision.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="active_for_sources",
                to="workbench.document",
            ),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="result_document",
            field=models.OneToOneField(
                blank=True,
                help_text="Immutable processed revision produced by this job.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="processing_job",
                to="workbench.document",
            ),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="worker_heartbeat_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="remote_poll_attempted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="remote_response_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="lease_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="worker_id",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="status_message",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="remote_status",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="consecutive_poll_errors",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="processingjob",
            name="state",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("submitting", "Submitting to processor"),
                    ("processing", "Processing"),
                    ("importing", "Importing results"),
                    ("submission_uncertain", "Submission uncertain"),
                    ("interrupted", "Interrupted"),
                    ("completed", "Completed"),
                    ("partial", "Partially completed"),
                    ("failed", "Failed"),
                    ("cancelled", "Cancelled"),
                ],
                default="queued",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="pageregion",
            name="page",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="regions",
                to="workbench.page",
            ),
        ),
        migrations.RunPython(backfill_revision_links, migrations.RunPython.noop),
    ]
