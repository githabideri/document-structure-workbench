"""Grant or update a user's membership role on a project.

Operators/automation run this on the deployed release through the audited
``dsw-ops-manage`` wrapper, so a real user can be given access to a project
without touching the administrator password or exposing a generic Django shell.

Idempotent: re-running with the same role is a no-op; a different role updates
the existing membership in place (never duplicates).

If the user does not exist yet, pass ``--create-user`` to create the account
(django-managed password) in the same call; ``--password`` optionally sets a
specific password instead of a generated one. The temporary password, when
created, is printed exactly once so the operator can hand it to the user.

Role ordering (lowest -> highest): viewer, reviewer, editor, owner.
"""
import random
import string

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from workbench.models import Collection, ProjectMembership

ROLE_CHOICES = [choice[0] for choice in ProjectMembership.ROLE_CHOICES]


def _generated_password():
    """Generate a strong random temporary password."""
    return "".join(random.choices(string.ascii_letters + string.digits, k=18))


class Command(BaseCommand):
    help = "Grant or update a user's membership role on a project (optionally creating the user)."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True, help="Django user username.")
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
        parser.add_argument(
            "--create-user",
            action="store_true",
            help="Create the user account if it does not exist (sets a temporary password).",
        )
        parser.add_argument(
            "--password",
            default="",
            help="Explicit password when --create-user is used (default: generated).",
        )

    def _resolve_project(self, value):
        if str(value).isdigit():
            by_id = Collection.objects.filter(pk=int(value)).first()
            if by_id:
                return by_id
        return Collection.objects.filter(name__iexact=value).first()

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"].strip()
        user = User.objects.filter(username=username).first()

        created_user = False
        password = ""
        if not user:
            if not options["create_user"]:
                raise CommandError(
                    "No user with username {!r} (pass --create-user to create it).".format(
                        username
                    )
                )
            user = User(username=username)
            password = options["password"].strip() or _generated_password()
            user.set_password(password)
            user.is_active = True
            try:
                user.full_clean()
            except Exception as exc:
                raise CommandError("Cannot create user: {}".format(exc))
            user.save()
            created_user = True
            self.stdout.write(
                "Created user '{}' (temporary password: {})".format(username, password)
            )

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
        action = "Created" if created else "Updated"
        self.stdout.write(
            "{} project memberships for '{}': {} ({}), role={}".format(
                action, username, project.name, project.id, membership.role,
            )
        )
        self.stdout.write(self.style.SUCCESS("ready"))
