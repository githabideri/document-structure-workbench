"""Remove the throwaway browser smoke-test editor user created by
``create_test_user``."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = "Remove the throwaway smoke-test editor user (and memberships)."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="dswtest")

    def handle(self, *args, **options):
        deleted, _ = User.objects.filter(username=options["username"]).delete()
        self.stdout.write("deleted %r" % deleted)
