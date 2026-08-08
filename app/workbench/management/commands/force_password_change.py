"""Set or clear a user's forced-password-change flag.

Run on the deployed release through the audited ``dsw-ops-manage`` wrapper. When
the flag is set (default), the user is forced to choose a new password on their
next login before they can use any other page (enforced by middleware).
``--clear`` removes the requirement.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from workbench.models import UserPreferences


class Command(BaseCommand):
    help = "Set or clear the forced-password-change flag for a user."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Clear the forced-password-change requirement.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        user = User.objects.filter(username=options["username"]).first()
        if not user:
            raise CommandError(
                "No user with username {!r}.".format(options["username"])
            )
        prefs = UserPreferences.get_or_create_for_user(user)
        prefs.must_change_password = not options["clear"]
        prefs.save(update_fields=["must_change_password"])
        state = "NOT required" if options["clear"] else "required"
        self.stdout.write(
            "Forced password change for '{}' is now {}.".format(
                user.get_username(), state
            )
        )
        self.stdout.write(self.style.SUCCESS("ready"))
