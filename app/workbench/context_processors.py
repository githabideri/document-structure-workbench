"""Shared context processors for the workbench."""
from pathlib import Path
from django.conf import settings


def release_info(request):
    """Provide release SHA to all templates."""
    release_sha = None
    release_file_path = getattr(settings, "RELEASE_FILE", "")
    if release_file_path:
        release_file = Path(release_file_path)
        if release_file.exists():
            release_sha = release_file.read_text().strip()
    return {"release_sha": release_sha, "release_sha_short": release_sha[:8] if release_sha else ""}


def user_roles(request):
    """Provide role booleans to all templates.

    Use these instead of comparing user.groups.name in templates.
    """
    if not request.user.is_authenticated:
        return {
            "is_reviewer": False,
            "is_curator": False,
            "is_admin": False,
            "has_review_work": False,
        }
    groups = request.user.groups.all()
    group_names = [g.name for g in groups]
    from .models import ReviewTask

    return {
        "is_reviewer": any(n in ("Reviewer", "Curator", "Administrator") for n in group_names),
        "is_curator": any(n in ("Curator", "Administrator") for n in group_names),
        "is_admin": "Administrator" in group_names,
        "has_review_work": ReviewTask.objects.filter(assigned_to=request.user).exists(),
    }
