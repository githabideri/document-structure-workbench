"""Shared, deliberately conservative handling of throwaway browser smoke-test
accounts.

The only accounts these helpers may touch live in a reserved, prefixed
namespace (``dswtest-*``). Any other username is refused. That makes it safe to
re-run repeatedly: an account inside the namespace is by definition one this
tooling created, so resetting its password or re-granting editor membership
cannot unexpectedly mutate a real user's account. Deletion is likewise limited
to the namespaced account being removed.
"""
import re

#: Reserved namespace for browser smoke-test accounts. Only usernames in this
#: namespace may be created, reused or removed by the smoke-test management
#: commands (see ``workbench/management/commands/create_test_user.py`` and
#: ``remove_test_user.py``).
SMOKE_USER_PREFIX = "dswtest"

#: Match ``dswtest`` or ``dswtest-<ident>`` (letters/digits/underscore/hyphen).
_SMOKE_USERNAME_RE = re.compile(r"^dswtest(?:-[A-Za-z0-9_-]+)?$")


def is_smoke_username(username: str) -> bool:
    """True when ``username`` lives in the reserved smoke-test namespace."""
    return bool(_SMOKE_USERNAME_RE.match(username or ""))
