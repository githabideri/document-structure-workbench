"""Remove a user's membership from a project.

Run on the deployed release through the audited ``dsw-ops-manage`` wrapper.
Idempotent: removing a membership that does not exist is a no-op (exit 0).

After removal, the user loses all access to that project unless they are a
global administrator.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from workbench.models import Collection, ProjectMembership


class Command(BaseCommand):
    help = "Remove a user's membership from a project."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True, help="Existing Django user username.")
        parser.add_argument(
            "--project",
            required=True,
            help="Project id or case-insensitive project name.",
        )

    def _resolve_project(self, value):
        if str(value).isdigit():
            by_id = Collection.objects.filter(pk=int(value)).first()
            if by_id:
                return by_id
        return Collection.objects.filter(name__iexact=value).first()

    def handle(self, *args, **options):
        User = get_user_model()
        user = User.objects.filter(username=options["username"]).first()
        if not user:
            raise CommandError("No user with username {!r}.".format(options["username"]))

        project = self._resolve_project(options["project"].strip())
        if not project:
            raise CommandError(
                "No project matching id/name {!r}.".format(options["project"])
            )

        deleted, _ = ProjectMembership.objects.filter(
            project=project, user=user
        ).delete()
        if deleted:
            self.stdout.write(
                "Removed '{}' from project '{}' ({}).".format(
                    user.get_username(), project.name, project.id
                )
            )
        else:
            self.stdout.write(
                "'{}' had no membership on project '{}' ({}); nothing to do.".format(
                    user.get_username(), project.name, project.id
                )
            )
        self.stdout.write(self.style.SUCCESS("ready"))
