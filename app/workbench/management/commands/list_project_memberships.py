"""List project memberships, for verification after granting/revoking access.

Run on the deployed release through the audited ``dsw-ops-manage`` wrapper.
Read-only. Exactly one of ``--username`` or ``--project`` (id/name) selects the
rows to show; with neither, all memberships are listed (useful for audits).

Prints ``<project>|<user>|<role>|<joined_at>`` per line plus a final ``ready``.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from workbench.models import Collection, ProjectMembership


class Command(BaseCommand):
    help = "List project memberships (user, project, role)."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="", help="Restrict to this user.")
        parser.add_argument(
            "--project",
            default="",
            help="Restrict to this project (id or case-insensitive name).",
        )

    def _resolve_project(self, value):
        if str(value).isdigit():
            by_id = Collection.objects.filter(pk=int(value)).first()
            if by_id:
                return by_id
        return Collection.objects.filter(name__iexact=value).first()

    def handle(self, *args, **options):
        qs = ProjectMembership.objects.select_related("project", "user").order_by(
            "project__name", "user__username"
        )

        if options["username"]:
            User = get_user_model()
            user = User.objects.filter(username=options["username"]).first()
            if not user:
                raise CommandError(
                    "No user with username {!r}.".format(options["username"])
                )
            qs = qs.filter(user=user)

        if options["project"]:
            project = self._resolve_project(options["project"].strip())
            if not project:
                raise CommandError(
                    "No project matching id/name {!r}.".format(options["project"])
                )
            qs = qs.filter(project=project)

        count = 0
        for membership in qs:
            count += 1
            self.stdout.write(
                "{}|{}|{}|{}".format(
                    membership.project.name,
                    membership.user.get_username(),
                    membership.role,
                    membership.joined_at.isoformat(),
                )
            )
        self.stdout.write("total={}".format(count))
        self.stdout.write(self.style.SUCCESS("ready"))
