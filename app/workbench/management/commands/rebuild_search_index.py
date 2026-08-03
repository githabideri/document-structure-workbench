from django.core.management.base import BaseCommand

from workbench.models import Document
from workbench.search import rebuild_revision_index


class Command(BaseCommand):
    help = "Rebuild revision-aware lexical search passages."

    def add_arguments(self, parser):
        parser.add_argument("--document", type=int)

    def handle(self, *args, **options):
        documents = Document.objects.all()
        if options.get("document"):
            documents = documents.filter(pk=options["document"])
        total = 0
        for document in documents.iterator():
            total += rebuild_revision_index(document)
        self.stdout.write(self.style.SUCCESS(f"Indexed {total} passages."))
