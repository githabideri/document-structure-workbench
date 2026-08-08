"""Remove a throwaway browser smoke-test editor user created by
``create_test_user``.

Deletion is strictly limited to the reserved ``dswtest*`` namespace. A username
must be supplied explicitly; leaving it off (or naming a non-namespaced account)
is refused so a real user can never be removed by accident.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from workbench.smoke_accounts import is_smoke_username

User = get_user_model()


class Command(BaseCommand):
    help = "Remove a throwaway dswtest* smoke-test editor user (and memberships)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--username", required=True,
            help="Exact dswtest* username to remove (must be in the reserved namespace).",
        )

    def handle(self, *args, **options):
        username = options["username"].strip()
        if not is_smoke_username(username):
            raise CommandError(
                f"Refusing to delete '{username}': only the reserved 'dswtest*' "
                "namespace may be removed by this smoke-test helper."
            )
        user = User.objects.filter(username=username).first()
        if not user:
            self.stdout.write(f"no such account: {username}")
            return
        deleted, _ = user.delete()  # memberships cascade via FK
        self.stdout.write(f"deleted {username} ({deleted} rows)")
