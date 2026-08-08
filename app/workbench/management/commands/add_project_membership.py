"""Grant or update a user's membership role on a project.

Operators/automation run this on the deployed release through the audited
``dsw-ops-manage`` wrapper, so a real user can be given access to a project
without touching the administrator password or exposing a generic Django shell.

Idempotent: re-running with the same role is a no-op; a different role updates
the existing membership in place (never duplicates).

Role ordering (lowest -> highest): viewer, reviewer, editor, owner.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from workbench.models import Collection, ProjectMembership

ROLE_CHOICES = [choice[0] for choice in ProjectMembership.ROLE_CHOICES]


class Command(BaseCommand):
    help = "Grant or update a user's membership role on a project."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True, help="Existing Django user username.")
        parser.add_argument(
            "--project",
            required=True,
            help="Project id or case-insensitive project name.",
        )
        parser.add_argument(
            "--role",
            default="viewer",
            choices=ROLE_CHOICES,
            help="Membership role (default: viewer).",
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

        role = options["role"]
        membership, created = ProjectMembership.objects.update_or_create(
            project=project,
            user=user,
            defaults={"role": role},
        )
        self.stdout.write(
            "{} project memberships for '{}': {} ({}), role={}".format(
                "Created" if created else "Updated",
                user.get_username(),
                project.name,
                project.id,
                membership.role,
            )
        )
        self.stdout.write(self.style.SUCCESS("ready"))
