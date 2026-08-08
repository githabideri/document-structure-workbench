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
from workbench.services import LifecycleError, ProjectLifecycleService

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
        # Pick any global-admin identity; try each candidate in order and use the
        # first that can actually edit the owning project.
        candidates = list(User.objects.filter(is_superuser=True).order_by("id"))
        candidates += list(User.objects.filter(groups__name="Administrator").order_by("id"))
        seen = set(); uniq = []
        for u in candidates:
            if u.pk in seen:
                continue
            seen.add(u.pk); uniq.append(u)
        for source in sources:
            source_is_superuser = False
            for admin in uniq:
                policy = ProjectAccessPolicy(user=admin)
                try:
                    if policy.can_edit(source.collection):
                        ProjectLifecycleService.archive_source(
                            source=source, policy=policy, archived=archived,
                        )
                        self.stdout.write(
                            "archived=%s source=%s project=%s (authorized_by=%s)" % (
                                archived, source.filename, source.collection.name, admin.username)
                        )
                        break
                except (PermissionError, LifecycleError) as exc:
                    self.stdout.write("candidate %s cannot edit: %s" % (admin.username, exc))
            else:
                detail = "; ".join(
                    "%s super=%s groups=%s" % (
                        u.username, u.is_superuser,
                        list(u.groups.values_list("name", flat=True)),
                    ) for u in uniq
                )
                proj = source.collection
                raise CommandError(
                    "No global-admin identity can edit project for %r. Admins: %s | project=%s archived=%s" % (
                        source.filename, detail or "none",
                        proj.name if proj else None,
                        proj.is_archived if proj else None,
                    )
                )
        for source in sources:
            ProjectLifecycleService.archive_source(
                source=source, policy=policy, archived=archived,
            )
            self.stdout.write(
                "archived=%s source=%s project=%s" % (archived, source.filename, source.collection.name)
            )
