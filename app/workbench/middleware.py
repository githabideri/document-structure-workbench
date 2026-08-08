"""
Middleware for user language preference persistence.

Respects the user's saved UI language preference (UserPreferences.ui_language).
For anonymous users, falls through to Django's LocaleMiddleware (accept-language / session).
"""
from django.utils import translation
from django.urls import reverse
from workbench.models import UserPreferences


class PasswordChangeRequiredMiddleware:
    """Force a user with ``must_change_password`` set to change their password.

    Every authenticated request is redirected to the forced password-change page
    until the user sets a new password (which clears the flag). A small allowlist
    keeps login/logout, static assets, admin, and the forced page itself
    reachable.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def _is_allowed(self, request):
        path = request.path
        if path == reverse("forced_password_change"):
            return True
        if path == reverse("logout"):
            return True
        if path.startswith("/static/") or path.startswith("/assets/"):
            return True
        if path.startswith("/admin/"):
            return True
        if path.startswith("/login/"):
            return True
        return False

    def __call__(self, request):
        if request.user.is_authenticated:
            prefs = UserPreferences.get_or_create_for_user(request.user)
            if prefs.must_change_password and not self._is_allowed(request):
                from django.shortcuts import redirect
                return redirect(reverse("forced_password_change"))
        return self.get_response(request)


class UserLanguageMiddleware:
    """Apply saved UI language for authenticated users.

    Also sets Content-Language header on all responses for authenticated users.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            prefs = UserPreferences.get_or_create_for_user(request.user)
            lang = prefs.ui_language
            if lang:
                translation.activate(lang)
                request.LANGUAGE_CODE = translation.get_language()
        response = self.get_response(request)
        # Set Content-Language header
        response["Content-Language"] = translation.get_language()
        return response
