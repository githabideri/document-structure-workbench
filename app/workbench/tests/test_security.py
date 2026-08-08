"""Tests for the cache-backed login brute-force throttle."""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

User = get_user_model()


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class LoginThrottleTests(TestCase):
    def setUp(self):
        cache.clear()

    def _user(self, suffix):
        u = User.objects.create_user("user-%s" % suffix, password="correct-password-123")
        return u.username

    def test_lockout_blocks_correct_credential_after_failures(self):
        username = self._user("lock")
        url = reverse("login")
        # Failures up to (limit-1) do not lock and do not redirect.
        for _ in range(7):
            resp = self.client.post(url, {"username": username, "password": "wrong"})
            self.assertEqual(resp.status_code, 200)
        # 8th failure triggers the lockout.
        resp = self.client.post(url, {"username": username, "password": "wrong"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Too many failed login attempts")
        # Even the correct password is now refused while locked.
        resp = self.client.post(url, {"username": username, "password": "correct-password-123"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Too many failed login attempts")

    def test_successful_login_clears_throttle(self):
        username = self._user("clear")
        url = reverse("login")
        for _ in range(3):
            self.client.post(url, {"username": username, "password": "wrong"})
        # Correct credential succeeds and redirects to dashboard.
        resp = self.client.post(url, {"username": username, "password": "correct-password-123"})
        self.assertEqual(resp.status_code, 302)
        # The throttle counter is cleared; a fresh failure no longer locks.
        for _ in range(7):
            resp = self.client.post(url, {"username": username, "password": "wrong"})
            self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Too many failed login attempts")

    def test_username_is_case_insensitive_for_lockout(self):
        username = self._user("case")
        url = reverse("login")
        for _ in range(8):
            self.client.post(url, {"username": username.upper(), "password": "wrong"})
        resp = self.client.post(url, {"username": username, "password": "correct-password-123"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Too many failed login attempts")
