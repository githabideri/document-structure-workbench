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
