"""Archive/unarchive a source document by filename (prototype hygiene).

Runs as a management command (via the audited dsw-ops-manage wrapper) so a
specific oversized/irrelevant document can be removed from active scope —
archiving stops it from appearing in searches/retrieval while preserving the
immutable revision. Reversible with ``--archived false``.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from workbench.models import SourceDocument
from workbench.policy import ProjectAccessPolicy
from workbench.services import ProjectLifecycleService

User = get_user_model()


class Command(BaseCommand):
    help = "Archive/unarchive a source document by exact filename."

    def add_arguments(self, parser):
        parser.add_argument("--filename", required=True)
        parser.add_argument("--archived", default="true", choices=["true", "false"])

    def handle(self, *args, **options):
        sources = list(SourceDocument.objects.filter(filename=options["filename"]))
        if not sources:
            raise CommandError("No source document matches filename %r" % options["filename"])
        archived = options["archived"] == "true"
        admin = User.objects.filter(is_superuser=True).first()
        policy = ProjectAccessPolicy(user=admin) if admin else None
        for source in sources:
            ProjectLifecycleService.archive_source(
                source=source, policy=policy, archived=archived,
            )
            self.stdout.write(
                "archived=%s source=%s project=%s" % (archived, source.filename, source.collection.name)
            )
