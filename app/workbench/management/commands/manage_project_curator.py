"""Add/remove/list members of the Project Curator group.

Curators can do all project lifecycle work (create, archive/restore, manage
projects). Existing users were seeded into the group by a data migration; this
command lets an administrator change membership on a running deployment,
programmatically or by hand through the audited ``dsw-ops-manage`` wrapper.

Usage:
    manage_project_curator --list
    manage_project_curator --add alice
    manage_project_curator --remove bob
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from workbench.models import PROJECT_CURATOR_GROUP

User = get_user_model()


class Command(BaseCommand):
    help = "Manage membership of the Project Curator group."

    def add_arguments(self, parser):
        parser.add_argument("--add", dest="add_user", help="Username to add to the Project Curator group.")
        parser.add_argument("--remove", dest="remove_user", help="Username to remove from the Project Curator group.")
        parser.add_argument("--list", action="store_true", help="List current Project Curator members.")

    def handle(self, *args, **options):
        group, _ = Group.objects.get_or_create(name=PROJECT_CURATOR_GROUP)

        if options["list"]:
            names = list(group.user_set.order_by("username").values_list("username", flat=True))
            self.stdout.write(f"Project Curator members ({len(names)}):")
            for name in names:
                self.stdout.write(f"  {name}")
            return

        if options["add_user"] and options["remove_user"]:
            raise CommandError("Use only one of --add or --remove at a time.")

        target = options["add_user"] or options["remove_user"]
        if not target:
            raise CommandError("Pass --add USER, --remove USER, or --list.")

        try:
            user = User.objects.get(username=target)
        except User.DoesNotExist:
            raise CommandError(f"User '{target}' does not exist.")

        if options["add_user"]:
            if user in group.user_set.all():
                self.stdout.write(f"{target} is already a Project Curator.")
            else:
                group.user_set.add(user)
                self.stdout.write(f"Added {target} to Project Curator.")
        else:
            if user not in group.user_set.all():
                self.stdout.write(f"{target} is not a Project Curator.")
            else:
                group.user_set.remove(user)
                self.stdout.write(f"Removed {target} from Project Curator.")