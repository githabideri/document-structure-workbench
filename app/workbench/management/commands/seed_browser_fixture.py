"""Create a generic, idempotent fixture for local or staging browser smoke tests."""
import hashlib
import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from workbench.models import (
    Collection, Document, Page, ProcessingJob, ProcessingPreset,
    ProjectMembership, SearchPassage, SourceDocument,
)

User = get_user_model()


class Command(BaseCommand):
    help = "Seed a generic project with processed source-a/source-b documents for browser smoke tests."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="dsw-browser-user")
        parser.add_argument("--password", required=True, help="Password for the dedicated browser test user.")
        parser.add_argument("--project", default="DSW Browser Fixture")
        parser.add_argument("--source", action="append", dest="sources", default=None,
                            help="Source filename; repeat for multiple documents (default: source-a.pdf, source-b.pdf).")
        parser.add_argument("--json", action="store_true", help="Print machine-readable fixture identifiers.")

    def handle(self, *args, **options):
        filenames = options["sources"] or ["source-a.pdf", "source-b.pdf"]
        if not filenames:
            raise CommandError("At least one --source is required.")
        if len(set(filenames)) != len(filenames):
            raise CommandError("Source filenames must be unique.")

        with transaction.atomic():
            user, _ = User.objects.get_or_create(username=options["username"], defaults={"is_active": True})
            user.set_password(options["password"])
            user.is_active = True
            user.save(update_fields=["password", "is_active"])
            project, _ = Collection.objects.get_or_create(
                name=options["project"],
                defaults={"source_type": "integration", "created_by": user},
            )
            ProjectMembership.objects.update_or_create(
                project=project, user=user,
                defaults={"role": "owner"},
            )
            preset, _ = ProcessingPreset.objects.get_or_create(
                slug="dsw-browser-fixture",
                defaults={"name": "DSW Browser Fixture"},
            )
            sources = []
            for index, filename in enumerate(filenames, 1):
                digest = hashlib.sha256(filename.encode()).hexdigest()
                source, _ = SourceDocument.objects.get_or_create(
                    collection=project, filename=filename,
                    defaults={"source_type": "integration", "sha256": digest, "uploaded_by": user},
                )
                source.sha256 = digest
                source.source_type = "integration"
                source.uploaded_by = user
                source.is_archived = False
                source.save(update_fields=["sha256", "source_type", "uploaded_by", "is_archived"])
                document, _ = Document.objects.get_or_create(
                    collection=project, external_id=f"dsw-browser-{index}",
                    defaults={"filename": filename, "sha256": digest, "page_count": 1},
                )
                document.filename = filename
                document.sha256 = digest
                document.page_count = 1
                document.save(update_fields=["filename", "sha256", "page_count"])
                page, _ = Page.objects.get_or_create(document=document, page_number=1)
                job, _ = ProcessingJob.objects.get_or_create(
                    source_document=source, preset=preset,
                    defaults={"state": "completed", "result_document": document, "created_by": user},
                )
                if job.result_document_id != document.id or job.state != "completed":
                    job.result_document = document
                    job.state = "completed"
                    job.save(update_fields=["result_document", "state"])
                source.active_document = document
                source.save(update_fields=["active_document"])
                SearchPassage.objects.update_or_create(
                    project=project, source_document=source, processed_revision=document,
                    processing_job=job, page=page, ordinal=0,
                    defaults={
                        "passage_type": "paragraph",
                        "text": f"This generic browser fixture discusses {filename} and shared archival evidence.",
                        "normalized_text": f"this generic browser fixture discusses {filename.lower()} and shared archival evidence.",
                    },
                )
                sources.append({"id": source.id, "filename": filename, "revision_id": document.id})

        result = {"username": user.username, "project_id": project.id, "sources": sources}
        if options["json"]:
            self.stdout.write(json.dumps(result))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Browser fixture ready: user={user.username} project={project.id} "
                f"sources={','.join(str(item['id']) for item in sources)}"
            ))
