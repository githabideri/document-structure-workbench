"""Read-only diagnostics for a source document.

Dumps the immutable processed revision(s), the ``page_text`` artifact (flagging
raw table markup embedded in the full-page text), and the registered table
candidates with their extractions' ``raw_html`` presence.

Run on the deployed release through the audited ``dsw-ops-manage`` wrapper so an
operator can inspect real production data without a generic shell. Read-only.
"""
from django.core.management.base import BaseCommand, CommandError

from workbench.models import (
    Document, ProcessingArtifact, SourceDocument, TableCandidate, TableExtraction,
)


class Command(BaseCommand):
    help = "Dump rendering diagnostics for a source document (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--source-id", type=int, default=None,
                            help="SourceDocument id (the /documents/<id>/ URL id).")
        parser.add_argument(
            "--filename",
            default="",
            help="Locate source document(s) by case-insensitive filename substring.",
        )

    def handle(self, *args, **options):
        source_id = options["source_id"]
        filename = options["filename"].strip()
        if not source_id and not filename:
            raise CommandError("Provide --source-id or --filename.")

        sources = SourceDocument.objects.all()
        if source_id:
            sources = sources.filter(pk=source_id)
        if filename:
            sources = sources.filter(filename__icontains=filename)
        sources = list(sources.order_by("id"))
        if not sources:
            raise CommandError(
                "No source document matches source-id={!r} filename={!r}.".format(
                    source_id, filename
                )
            )

        for source in sources:
            self._dump(source)
        self.stdout.write(self.style.SUCCESS("ready"))

    def _dump(self, source):
        self.stdout.write("source id={} filename={!r} project={!r}".format(
            source.id, source.filename, source.collection.name if source.collection else None,
        ))

        documents = Document.objects.filter(processing_job__source_document=source)
        self.stdout.write("revisions: {}".format(list(documents.values_list("id", flat=True))))

        for document in documents:
            self.stdout.write("-- revision {} --".format(document.id))
            for artifact in ProcessingArtifact.objects.filter(
                job__result_document=document, artifact_type="page_text",
            ).order_by("page_number"):
                text = artifact.data.get("text", "") if isinstance(artifact.data, dict) else ""
                has_table = "<table" in text.lower() or "border=" in text.lower()
                self.stdout.write(
                    "  page_text page={} len={} contains_table_markup={}".format(
                        artifact.page_number, len(text), has_table,
                    )
                )
                if has_table:
                    idx = text.lower().find("<table")
                    self.stdout.write("    sample: {!r}".format(text[idx:idx + 160]))

        candidates = TableCandidate.objects.filter(document__in=documents).select_related("page")
        self.stdout.write("table candidates: {}".format(candidates.count()))
        for table in candidates:
            extractions = table.extractions.select_related("extraction_run")
            info = []
            for ex in extractions:
                info.append(
                    "run={} status={} raw_html_len={}".format(
                        ex.extraction_run_id, ex.status, len(ex.raw_html or ""),
                    )
                )
            self.stdout.write(
                "  T id={} page={} stable={} extractions=[{}]".format(
                    table.id, table.page.page_number if table.page else None,
                    table.stable_table_id, ", ".join(info) or "none",
                )
            )
