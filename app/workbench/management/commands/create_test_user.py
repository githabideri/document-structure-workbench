"""Create a throwaway editor user for browser smoke tests.

Acquired by operators/automation on the deployed release (via the audited
``dsw-ops-manage`` wrapper) so verification can authenticate as a real editor
without touching the administrator password. The account is active and gets a
real ``editor`` membership on each named project (default: every accessible,
non-archived collection).

Safety contract
---------------
Only usernames in the reserved ``dswtest*`` namespace (see
``workbench.smoke_accounts``) are ever touched. A newly-created account always
gets a fresh, unique username. Reusing an existing namespaced account is allowed
only with an explicit ``--reuse`` flag; reusing resets that account's password,
so it must be an account this tooling created. No other username is accepted,
which guarantees a real user's password is never reset by accident.

The generated password is printed ONCE to stdout for the operator's browser
session and is never written to application diagnostics/log metadata.
"""
import random
import string

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from workbench.models import Collection, ProjectMembership
from workbench.smoke_accounts import is_smoke_username

User = get_user_model()


class Command(BaseCommand):
    help = "Create/refresh a throwaway namespaced editor user for browser smoke tests."

    def add_arguments(self, parser):
        parser.add_argument(
            "--username", default="",
            help="Desired dswtest* username. Omit to generate a unique one.",
        )
        parser.add_argument(
            "--reuse", action="store_true",
            help="Allow resetting an EXISTING dswtest* account's password (explicit opt-in).",
        )
        parser.add_argument("--password", default="")
        parser.add_argument(
            "--projects", default="",
            help="comma-separated project names; default = all non-archived projects",
        )

    def _generate_username(self):
        while True:
            suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
            candidate = f"dswtest-{suffix}"
            if not User.objects.filter(username=candidate).exists():
                return candidate

    def handle(self, *args, **options):
        username = options["username"].strip()
        reuse = options["reuse"]
        password = options["password"].strip() or "".join(
            random.choices(string.ascii_letters + string.digits, k=18)
        )

        preexisting = None
        if username:
            if not is_smoke_username(username):
                raise CommandError(
                    f"Refusing to manage '{username}': only the reserved 'dswtest*' "
                    "namespace may be used by this smoke-test helper."
                )
            preexisting = User.objects.filter(username=username).first()
            if preexisting and not reuse:
                raise CommandError(
                    f"User '{username}' already exists. Pass --reuse to explicitly "
                    "reset this smoke-test account's password, or omit --username "
                    "to create a fresh unique account."
                )
        else:
            username = self._generate_username()

        if preexisting:
            user = preexisting
            created = False
        else:
            user, created = User.objects.get_or_create(username=username)
            if not created:
                # A generated name collided with a fresh account; treat as reuse
                # failure and refuse rather than silently resetting.
                raise CommandError(
                    f"Username '{username}' already exists; refusing to reset it without --reuse."
                )

        user.set_password(password)
        user.is_active = True
        user.is_staff = False
        user.is_superuser = False
        user.save()

        project_names = [p.strip() for p in options["projects"].split(",") if p.strip()]
        projects = list(Collection.objects.filter(is_archived=False))
        if project_names:
            projects = [p for p in projects if p.name in project_names]
        for project in projects:
            # update_or_create guarantees an existing lower-role membership is
            # promoted to editor, not silently left as reviewer/viewer.
            ProjectMembership.objects.update_or_create(
                project=project, user=user,
                defaults={"role": "editor"},
            )

        self.stdout.write(f"username={username}")
        self.stdout.write(f"password={password}")
        self.stdout.write(f"projects={', '.join(sorted(p.name for p in projects))}")
        self.stdout.write(self.style.SUCCESS("ready"))
