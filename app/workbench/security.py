"""Login brute-force throttle.

A small, dependency-free lockout on top of Django's login view, keyed by the
normalized username being attacked. It uses the default (locmem) cache, so no
third-party package, database table or migration is required and the behavior
is fully deterministic for tests.

Keying primarily on username (rather than client IP) is deliberate: behind the
TLS-terminating Caddy proxy all clients share the proxy IP, and blindly trusting
a client-supplied X-Forwarded-For for per-IP lockout would be spoofable. The
username is the real identity under attack. The lockout is per-process for the
locmem cache, which is acceptable for this single-application prototype; swap
``CACHES`` for a shared backend (or django-axes) if multi-process enforcement is
needed later.
"""
import hashlib
import time

from django.conf import settings
from django.contrib.auth import views as auth_views
from django.core.cache import cache


def _now():
    return time.time()


def _sha(value, prefix):
    return "%s-%s" % (prefix, hashlib.sha256((value or "").strip().casefold().encode("utf-8")).hexdigest()[:16])


def login_lockout_seconds(username):
    """Remaining lockout seconds for ``username`` (0 if not locked)."""
    until = cache.get(_sha(username, "dsw-login-lock"))
    if not until:
        return 0
    remaining = until - _now()
    return 0 if remaining <= 0 else int(remaining)


def _fail_counter_key(username):
    return _sha(username, "dsw-login-fails")


def record_failure(username):
    """Increment the failure counter; return True when it crosses the limit."""
    key = _fail_counter_key(username)
    count = cache.get(key, 0) + 1
    limit = max(1, int(getattr(settings, "LOGIN_THROTTLE_FAILURE_LIMIT", 8)))
    cache.set(key, count, timeout=max(1, int(getattr(settings, "LOGIN_THROTTLE_COOLDOWN_SECONDS", 900))))
    if count >= limit:
        until = _now() + max(1, int(getattr(settings, "LOGIN_THROTTLE_COOLDOWN_SECONDS", 900)))
        cache.set(_sha(username, "dsw-login-lock"), until, timeout=max(1, int(getattr(settings, "LOGIN_THROTTLE_COOLDOWN_SECONDS", 900))))
        cache.delete(key)
        return True
    return False


def clear_failures(username):
    cache.delete(_fail_counter_key(username))
    cache.delete(_sha(username, "dsw-login-lock"))


class ThrottledLoginView(auth_views.LoginView):
    """LoginView that refuses after too many failed attempts per username."""

    def _username(self):
        return (self.request.POST.get("username") or "").strip()

    def _lockout_message(self):
        minutes = max(1, round(login_lockout_seconds(self._username()) / 60))
        return "Too many failed login attempts. Try again in approximately %d minute(s)." % minutes

    def form_invalid(self, form):
        username = self._username()
        # While locked, do not even count further attempts; keep surfacing the
        # lockout message until the cooldown expires.
        if login_lockout_seconds(username):
            form.add_error(None, self._lockout_message())
            return self.render_to_response(self.get_context_data(form=form))
        if record_failure(username):
            form.add_error(None, self._lockout_message())
        return self.render_to_response(self.get_context_data(form=form))

    def form_valid(self, form):
        # A lockout must block even a correct credential until it expires.
        if login_lockout_seconds(self._username()):
            form.add_error(None, self._lockout_message())
            return self.render_to_response(self.get_context_data(form=form))
        clear_failures(self._username())
        return super().form_valid(form)
