"""Create an idempotent, clearly-named editor user for browser smoke tests.

Used by operators/automation on the deployed release (via the audited
``dsw-ops-manage`` wrapper) so verification can authenticate as a real editor
without touching the administrator password. Ensures the user is active and has
editor membership on each named project (default: every accessible collection).
Prints the generated password ONCE so it can be used for a browser session.
"""
import random
import string

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from workbench.models import Collection, ProjectMembership

User = get_user_model()


class Command(BaseCommand):
    help = "Create/refresh a throwaway editor user for browser smoke tests."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="dswtest")
        parser.add_argument("--password", default="")
        parser.add_argument("--projects", default="", help="comma-separated project names; default = all")

    def handle(self, *args, **options):
        username = options["username"].strip()
        password = options["password"].strip() or "".join(
            random.choices(string.ascii_letters + string.digits, k=18)
        )
        user, created = User.objects.get_or_create(username=username)
        user.set_password(password)
        user.is_active = True
        user.save()
        project_names = [p.strip() for p in options["projects"].split(",") if p.strip()]
        projects = list(Collection.objects.all())
        if project_names:
            projects = [p for p in projects if p.name in project_names]
        for project in projects:
            ProjectMembership.objects.get_or_create(
                project=project, user=user, defaults={"role": "editor"},
            )
        self.stdout.write("username=%s" % username)
        self.stdout.write("password=%s" % password)
        self.stdout.write("projects=%s" % ", ".join(sorted(p.name for p in projects)))
        self.stdout.write(self.style.SUCCESS("ready"))
