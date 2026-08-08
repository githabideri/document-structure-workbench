"""Tests for the forced password-change flow (middleware, view, command)."""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from workbench.models import UserPreferences

User = get_user_model()


# Avoid requiring a compiled staticfiles manifest for tests that render pages
# (mirrors existing suite behaviour in app/workbench/tests/test_processing.py).
_NON_MANIFEST_STORAGES = {
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _prefs(user):
    return UserPreferences.get_or_create_for_user(user)


def _set_flag(user, value):
    prefs = _prefs(user)
    prefs.must_change_password = value
    prefs.save(update_fields=["must_change_password"])
    return prefs


@override_settings(STORAGES=_NON_MANIFEST_STORAGES)
class ForcedPasswordChangeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="fritz", password="oldpass")
        self.client.force_login(self.user)

    def test_flagged_user_redirected_to_forced_page(self):
        _set_flag(self.user, True)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("forced_password_change"), response.url)

    def test_unflagged_user_not_redirected(self):
        _set_flag(self.user, False)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_forced_page_renders(self):
        _set_flag(self.user, True)
        response = self.client.get(reverse("forced_password_change"))
        self.assertEqual(response.status_code, 200)

    def test_success_clears_flag_and_allows_through(self):
        _set_flag(self.user, True)
        response = self.client.post(
            reverse("forced_password_change"),
            {
                "current_password": "oldpass",
                "new_password": "brandnewpass9",
                "confirm_password": "brandnewpass9",
            },
        )
        self.assertFalse(_prefs(self.user).must_change_password)
        self.assertTrue(
            User.objects.get(pk=self.user.pk).check_password("brandnewpass9")
        )
        # After the change, normal pages are reachable again.
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_wrong_current_password_rejected(self):
        _set_flag(self.user, True)
        response = self.client.post(
            reverse("forced_password_change"),
            {
                "current_password": "wrong",
                "new_password": "brandnewpass9",
                "confirm_password": "brandnewpass9",
            },
        )
        self.assertTrue(_prefs(self.user).must_change_password)
        self.assertEqual(response.status_code, 200)

    def test_short_new_password_rejected(self):
        _set_flag(self.user, True)
        self.client.post(
            reverse("forced_password_change"),
            {
                "current_password": "oldpass",
                "new_password": "short",
                "confirm_password": "short",
            },
        )
        self.assertTrue(_prefs(self.user).must_change_password)


class ForcePasswordChangeCommandTests(TestCase):
    def test_set_and_clear_flag(self):
        user = User.objects.create_user(username="florian", password="x")
        out = StringIO()
        call_command("force_password_change", "--username", "florian", stdout=out)
        self.assertTrue(_prefs(user).must_change_password)
        self.assertIn("required", out.getvalue())

        call_command("force_password_change", "--username", "florian", "--clear", stdout=out)
        self.assertFalse(_prefs(user).must_change_password)
        self.assertIn("required", out.getvalue())
