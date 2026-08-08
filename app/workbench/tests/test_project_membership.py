"""Tests for the project-membership management commands and admin wiring."""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from workbench.models import Collection, ProjectMembership

User = get_user_model()


def _run(*args, **kwargs):
    out = StringIO()
    call_command(*args, stdout=out, **kwargs)
    return out.getvalue()


class AddProjectMembershipCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="martin")
        self.project = Collection.objects.create(name="Alpha")

    def test_creates_membership_with_role(self):
        out = _run(
            "add_project_membership",
            "--username", "martin",
            "--project", "Alpha",
            "--role", "editor",
        )
        self.assertIn("role=editor", out)
        self.assertIn("ready", out)
        membership = ProjectMembership.objects.get(project=self.project, user=self.user)
        self.assertEqual(membership.role, "editor")

    def test_resolves_project_by_id(self):
        _run(
            "add_project_membership",
            "--username", "martin",
            "--project", str(self.project.id),
            "--role", "viewer",
        )
        self.assertTrue(
            ProjectMembership.objects.filter(
                project=self.project, user=self.user, role="viewer"
            ).exists()
        )

    def test_updates_role_idempotently_no_duplicate(self):
        ProjectMembership.objects.create(project=self.project, user=self.user, role="viewer")
        _run(
            "add_project_membership",
            "--username", "martin",
            "--project", "Alpha",
            "--role", "owner",
        )
        memberships = ProjectMembership.objects.filter(project=self.project, user=self.user)
        self.assertEqual(memberships.count(), 1)
        self.assertEqual(memberships.first().role, "owner")

    def test_missing_user_raises(self):
        with self.assertRaises(CommandError):
            _run("add_project_membership", "--username", "nobody", "--project", "Alpha")

    def test_missing_project_raises(self):
        with self.assertRaises(CommandError):
            _run("add_project_membership", "--username", "martin", "--project", "Nope")

    def test_creates_user_and_membership_in_one_call(self):
        User = get_user_model()
        self.assertFalse(User.objects.filter(username="eva").exists())
        out = _run(
            "add_project_membership",
            "--username", "eva",
            "--project", "Alpha",
            "--role", "editor",
            "--create-user",
            "--password", "temp-pass-123",
        )
        self.assertIn("Created user 'eva'", out)
        self.assertIn("role=editor", out)
        eva = User.objects.get(username="eva")
        self.assertTrue(eva.is_active)
        self.assertTrue(eva.check_password("temp-pass-123"))
        self.assertTrue(
            ProjectMembership.objects.filter(
                project=self.project, user=eva, role="editor"
            ).exists()
        )

    def test_create_user_requires_flag_when_user_missing(self):
        with self.assertRaises(CommandError):
            _run("add_project_membership", "--username", "nova", "--project", "Alpha")


class RemoveProjectMembershipCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="martin")
        self.project = Collection.objects.create(name="Alpha")

    def test_removes_existing_membership(self):
        ProjectMembership.objects.create(project=self.project, user=self.user, role="editor")
        out = _run(
            "remove_project_membership", "--username", "martin", "--project", "Alpha"
        )
        self.assertIn("Removed", out)
        self.assertFalse(ProjectMembership.objects.filter(project=self.project, user=self.user).exists())

    def test_removing_missing_membership_is_noop(self):
        out = _run(
            "remove_project_membership", "--username", "martin", "--project", "Alpha"
        )
        self.assertIn("nothing to do", out)


class ListProjectMembershipsCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="martin")
        self.other = User.objects.create_user(username="eva")
        self.project = Collection.objects.create(name="Alpha")
        ProjectMembership.objects.create(project=self.project, user=self.user, role="owner")
        ProjectMembership.objects.create(project=self.project, user=self.other, role="viewer")

    def test_list_all(self):
        out = _run("list_project_memberships")
        self.assertIn("martin|owner", out.replace("Alpha|", ""))
        self.assertIn("eva|viewer", out)
        self.assertIn("total=2", out)

    def test_filter_by_user(self):
        out = _run("list_project_memberships", "--username", "martin")
        self.assertIn("martin", out)
        self.assertNotIn("eva", out)
        self.assertIn("total=1", out)

    def test_filter_by_project(self):
        out = _run("list_project_memberships", "--project", "Alpha")
        self.assertIn("total=2", out)


class AdminRegistrationTests(TestCase):
    def test_project_membership_registered(self):
        from django.contrib import admin

        self.assertIsNotNone(admin.site._registry.get(ProjectMembership))
        self.assertIsNotNone(admin.site._registry.get(Collection))
        # The project page exposes an inline for managing members.
        collection_admin = admin.site._registry[Collection]
        self.assertTrue(any(
            getattr(inline, "model", None) is ProjectMembership
            for inline in collection_admin.inlines
        ))
