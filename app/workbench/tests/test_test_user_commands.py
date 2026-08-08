"""Tests for the hardened browser smoke-test user management commands.

Covers the safety contract: only the reserved ``dswtest*`` namespace is ever
touched, an existing account is never reset without an explicit ``--reuse``, and
an existing lower-role membership is actually promoted to editor.
"""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from workbench.models import Collection, ProjectMembership

User = get_user_model()


class CreateTestUserCommandTests(TestCase):
    def setUp(self):
        self.project = Collection.objects.create(name="Corpus A")
        self.other = Collection.objects.create(name="Corpus B", is_archived=True)

    def call(self, *args, **kwargs):
        out = StringIO()
        call_command("create_test_user", *args, stdout=out, **kwargs)
        return out.getvalue()

    def test_fresh_unique_namespaced_editor_user(self):
        out = self.call("--password", "supersecret-pass-1")
        username = next(l for l in out.splitlines() if l.startswith("username=")).split("=", 1)[1]
        self.assertTrue(username.startswith("dswtest"))
        user = User.objects.get(username=username)
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.check_password("supersecret-pass-1"))
        # Editor on the non-archived project; the archived one is excluded.
        mem = ProjectMembership.objects.get(project=self.project, user=user)
        self.assertEqual(mem.role, "editor")
        self.assertFalse(ProjectMembership.objects.filter(project=self.other, user=user).exists())

    def test_refuses_non_namespaced_username(self):
        with self.assertRaises(CommandError):
            self.call("--username", "alice")

    def test_refuses_existing_account_without_reuse(self):
        existing = User.objects.create_user("dswtest-occupied", password="old-pass")
        with self.assertRaises(CommandError):
            self.call("--username", "dswtest-occupied", "--password", "new-pass")
        existing.refresh_from_db()
        # Password was NOT reset.
        self.assertTrue(existing.check_password("old-pass"))

    def test_reuse_promotes_existing_lower_role_to_editor(self):
        existing = User.objects.create_user("dswtest-reuse", password="old-pass")
        ProjectMembership.objects.create(project=self.project, user=existing, role="viewer")
        self.call("--username", "dswtest-reuse", "--reuse", "--password", "new-pass")
        existing.refresh_from_db()
        self.assertTrue(existing.check_password("new-pass"))
        mem = ProjectMembership.objects.get(project=self.project, user=existing)
        self.assertEqual(mem.role, "editor")

    def test_reuse_does_not_affect_other_accounts(self):
        other = User.objects.create_user("dswtest-another", password="keep-me")
        self.call("--username", "dswtest-another", "--reuse", "--password", "changed")
        # A different non-touched account is unaffected.
        untouched = User.objects.create_user("real-user", password="real-pass")
        untouched.refresh_from_db()
        self.assertTrue(untouched.check_password("real-pass"))


class RemoveTestUserCommandTests(TestCase):
    def test_requires_explicit_namespaced_username(self):
        with self.assertRaises(CommandError):
            call_command("remove_test_user")  # no --username
        with self.assertRaises(CommandError):
            call_command("remove_test_user", "--username", "bob")

    def test_deletes_only_intended_smoke_account(self):
        smoke = User.objects.create_user("dswtest-remove-me", password="x")
        real = User.objects.create_user("keep-me", password="y")
        call_command("remove_test_user", "--username", "dswtest-remove-me")
        self.assertFalse(User.objects.filter(username="dswtest-remove-me").exists())
        self.assertTrue(User.objects.filter(username="keep-me").exists())
        self.assertTrue(User.objects.filter(pk=real.pk).exists())

    def test_noop_when_missing(self):
        call_command("remove_test_user", "--username", "dswtest-does-not-exist")  # no error
