"""Provision and reset the isolated, synthetic workspace browser canary.

This command is intentionally scoped to the reserved ``dsw-e2e-*`` identity and
``E2E Workspace Canary`` project. It never searches for or mutates ordinary
accounts/projects. The password is read only from the named environment
variable when explicitly supplied; it is never printed or stored by the
command as plaintext.
"""
import json
import os
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from workbench.models import (
    Collection,
    Document,
    OcrRequest,
    Page,
    PageRegion,
    ProcessingJob,
    ProcessingPreset,
    ProjectMembership,
    SearchPassage,
    SourceDocument,
    RegionCorrection,
)

User = get_user_model()

DEFAULT_USERNAME = "dsw-e2e-workspace"
PROJECT_NAME = "E2E Workspace Canary"
SOURCE_EXTERNAL_ID = "e2e-workspace-canary"
SOURCE_FILENAME = "e2e-workspace-canary.png"
PRESET_SLUG = "dsw-e2e-workspace"
MARKER = "workspace-e2e"
LOGICAL_SIZE = (595, 841)
RASTER_SIZE = (1190, 1682)

REGIONS = [
    {
        "key": "small",
        "bbox": (0.10, 0.12, 0.32, 0.20),
        "text": "Synthetic small region: Kurrent ledger note.",
    },
    {
        "key": "medium",
        "bbox": (0.42, 0.30, 0.88, 0.48),
        "text": "Synthetic medium region: the clerk recorded the shipment at dusk.",
    },
    {
        "key": "large",
        "bbox": (0.06, 0.22, 0.94, 0.82),
        "text": "Synthetic large region: this enclosing canary paragraph exercises overlap and hit testing.",
    },
]


class Command(BaseCommand):
    help = "Provision or reset the isolated E2E Workspace Canary fixture."

    def add_arguments(self, parser):
        parser.add_argument("--username", default=DEFAULT_USERNAME)
        parser.add_argument(
            "--password-env", default="DSW_E2E_PASSWORD",
            help="Environment variable containing the password (never printed).",
        )
        parser.add_argument("--reset", action="store_true", help="Restore the canary baseline after provisioning.")
        parser.add_argument("--json", action="store_true", help="Print safe machine-readable identifiers.")

    def handle(self, *args, **options):
        username = options["username"].strip()
        if username != DEFAULT_USERNAME:
            raise CommandError(
                f"Refusing username {username!r}; this command is reserved for {DEFAULT_USERNAME!r}."
            )
        password_env = options["password_env"].strip()
        password = os.environ.get(password_env, "")

        with transaction.atomic():
            user = self._ensure_user(username, password, password_env)
            project = self._ensure_project(user)
            ProjectMembership.objects.update_or_create(
                project=project, user=user, defaults={"role": "editor"},
            )
            source, document, page, regions = self._ensure_fixture(user, project)
            if options["reset"]:
                self._reset_fixture(project, document, regions)

        result = {
            "username": user.username,
            "project_id": project.id,
            "source_id": source.id,
            "revision_id": document.id,
            "page": page.page_number,
            "region_a": regions[0].id,
            "region_b": regions[1].id,
            "region_c": regions[2].id,
        }
        if options["json"]:
            self.stdout.write(json.dumps(result, sort_keys=True))
        else:
            self.stdout.write(self.style.SUCCESS(
                "E2E workspace ready: "
                + " ".join(f"{key}={value}" for key, value in result.items())
            ))

    def _ensure_user(self, username, password, password_env):
        user, created = User.objects.get_or_create(
            username=username, defaults={"is_active": True, "is_staff": False, "is_superuser": False},
        )
        if created and not password:
            raise CommandError(
                f"New E2E user requires its password in ${password_env}; no secret was supplied."
            )
        if user.is_superuser or user.is_staff:
            raise CommandError("The reserved E2E user must not be staff or superuser.")
        changed = False
        if not user.is_active:
            user.is_active = True
            changed = True
        if password and not user.check_password(password):
            user.set_password(password)
            changed = True
        if changed:
            user.save(update_fields=["password", "is_active"] if password else ["is_active"])
        return user

    def _ensure_project(self, user):
        project = Collection.objects.filter(name=PROJECT_NAME).first()
        if project is None:
            project = Collection.objects.create(
                name=PROJECT_NAME,
                description="Synthetic only; purpose=workspace-e2e; do not use for archival data.",
                source_type="integration",
                created_by=user,
            )
        elif MARKER not in project.description:
            raise CommandError(
                f"Refusing project {PROJECT_NAME!r}: it is not marked as the synthetic E2E canary."
            )
        if project.is_archived:
            project.is_archived = False
            project.save(update_fields=["is_archived"])
        return project

    def _ensure_fixture(self, user, project):
        base = Path(getattr(settings, "ARTIFACTS_BASE_DIR", "/var/lib/dsw/artifacts"))
        rel_image = "e2e-workspace-canary/page-0001.png"
        image_path = base / rel_image
        self._write_fixture_image(image_path)

        source, _ = SourceDocument.objects.get_or_create(
            collection=project, filename=SOURCE_FILENAME,
            defaults={
                "source_type": "batch_import", "sha256": "e2e-workspace-canary",
                "uploaded_by": user, "page_count": 1,
            },
        )
        source.source_type = "batch_import"
        source.sha256 = "e2e-workspace-canary"
        source.page_count = 1
        source.uploaded_by = user
        source.is_archived = False
        source.save(update_fields=["source_type", "sha256", "page_count", "uploaded_by", "is_archived"])

        document, _ = Document.objects.get_or_create(
            collection=project, external_id=SOURCE_EXTERNAL_ID,
            defaults={
                "filename": SOURCE_FILENAME, "sha256": "e2e-workspace-canary",
                "page_count": 1, "metadata": {"synthetic": True, "purpose": MARKER},
            },
        )
        document.filename = SOURCE_FILENAME
        document.sha256 = "e2e-workspace-canary"
        document.page_count = 1
        document.metadata = {"synthetic": True, "purpose": MARKER}
        document.is_archived = False
        document.save(update_fields=["filename", "sha256", "page_count", "metadata", "is_archived"])

        preset, _ = ProcessingPreset.objects.get_or_create(
            slug=PRESET_SLUG, defaults={"name": "E2E Workspace Canary"},
        )
        job, _ = ProcessingJob.objects.get_or_create(
            source_document=source, preset=preset,
            defaults={"state": "completed", "result_document": document, "created_by": user},
        )
        if job.result_document_id != document.id or job.state != "completed":
            job.result_document = document
            job.state = "completed"
            job.created_by = user
            job.save(update_fields=["result_document", "state", "created_by"])
        source.active_document = document
        source.save(update_fields=["active_document"])

        page, _ = Page.objects.get_or_create(document=document, page_number=1)
        page.image_path = rel_image
        page.width, page.height = LOGICAL_SIZE
        page.image_width, page.image_height = RASTER_SIZE
        page.save(update_fields=["image_path", "width", "height", "image_width", "image_height"])

        regions = []
        for item in REGIONS:
            left, top, right, bottom = item["bbox"]
            region, _ = PageRegion.objects.update_or_create(
                source_document=source, job=job, page=page, page_number=1,
                metadata__e2e_key=item["key"],
                defaults={
                    "region_type": "text", "left": left, "top": top,
                    "right": right, "bottom": bottom, "text": item["text"],
                    "page_width": LOGICAL_SIZE[0], "page_height": LOGICAL_SIZE[1],
                    "metadata": {"synthetic": True, "purpose": MARKER, "e2e_key": item["key"]},
                },
            )
            regions.append(region)
            SearchPassage.objects.update_or_create(
                project=project, source_document=source, processed_revision=document,
                processing_job=job, page=page, page_region=region, ordinal=region.id,
                defaults={
                    "passage_type": "region", "text": item["text"],
                    "normalized_text": item["text"].lower(),
                },
            )
        return source, document, page, regions

    def _reset_fixture(self, project, document, regions):
        RegionCorrection.objects.filter(document__collection=project).delete()
        OcrRequest.objects.filter(document__collection=project).delete()
        for item, region in zip(REGIONS, regions):
            region.text = item["text"]
            region.region_type = "text"
            region.metadata = {"synthetic": True, "purpose": MARKER, "e2e_key": item["key"]}
            region.save(update_fields=["text", "region_type", "metadata"])
        SearchPassage.objects.filter(project=project).update(
            text="", normalized_text="",
        )
        # Rebuild only this fixture's passages, not the user's other projects.
        for region in regions:
            SearchPassage.objects.update_or_create(
                project=project, source_document=region.source_document,
                processed_revision=document, processing_job=region.job,
                page=region.page, page_region=region, ordinal=region.id,
                defaults={"passage_type": "region", "text": region.text,
                          "normalized_text": region.text.lower()},
            )

    @staticmethod
    def _write_fixture_image(path):
        from PIL import Image, ImageDraw

        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", RASTER_SIZE, (250, 250, 248))
        draw = ImageDraw.Draw(image)
        for x in range(0, RASTER_SIZE[0], 42):
            draw.line((x, 0, x, RASTER_SIZE[1]), fill=(224, 224, 220), width=1)
        for y in range(0, RASTER_SIZE[1], 42):
            draw.line((0, y, RASTER_SIZE[0], y), fill=(224, 224, 220), width=1)
        for item in REGIONS:
            left, top, right, bottom = item["bbox"]
            draw.rectangle(
                (int(left * RASTER_SIZE[0]), int(top * RASTER_SIZE[1]),
                 int(right * RASTER_SIZE[0]), int(bottom * RASTER_SIZE[1])),
                outline=(35, 100, 165), width=4,
            )
        image.save(path, "PNG")
