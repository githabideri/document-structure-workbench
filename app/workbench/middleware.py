"""
Middleware for user language preference persistence.

Respects the user's saved UI language preference (UserPreferences.ui_language).
For anonymous users, falls through to Django's LocaleMiddleware (accept-language / session).
"""
from django.utils import translation
from workbench.models import UserPreferences


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
