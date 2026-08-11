import json
import os
import tempfile
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from workbench.models import OcrRequest, ProjectMembership, RegionCorrection

User = get_user_model()


@override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
class ProvisionE2EWorkspaceTests(TestCase):
    def run_command(self, *args):
        out = StringIO()
        with patch.dict(os.environ, {"DSW_E2E_PASSWORD": "synthetic-only-password"}, clear=False):
            call_command("provision_e2e_workspace", *args, "--json", stdout=out)
        return json.loads(out.getvalue())

    def test_provision_is_idempotent_and_least_privilege(self):
        first = self.run_command()
        second = self.run_command()
        self.assertEqual(first, second)
        self.assertEqual(User.objects.filter(username="dsw-e2e-workspace").count(), 1)
        self.assertEqual(ProjectMembership.objects.filter(project_id=first["project_id"]).count(), 1)
        self.assertEqual(ProjectMembership.objects.get(project_id=first["project_id"]).role, "editor")
        user = User.objects.get(username="dsw-e2e-workspace")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(OcrRequest.objects.count(), 0)

    def test_reset_removes_fixture_corrections_and_candidates(self):
        ids = self.run_command()
        from workbench.models import Document, PageRegion

        document = Document.objects.get(pk=ids["revision_id"])
        region = PageRegion.objects.get(pk=ids["region_a"])
        user = User.objects.get(username="dsw-e2e-workspace")
        correction = RegionCorrection.objects.create(
            region=region, document=document, created_by=user, operation="text",
            before={"text": region.text}, after={"text": "changed"},
        )
        OcrRequest.objects.create(
            source_document=document.processing_job.source_document,
            document=document, page=region.page, region=region, prompt="synthetic",
            state="completed", candidate_text="candidate", created_by=user,
        )
        self.run_command("--reset")
        self.assertFalse(RegionCorrection.objects.filter(pk=correction.pk).exists())
        self.assertEqual(OcrRequest.objects.count(), 0)
        region.refresh_from_db()
        self.assertIn("Synthetic small region", region.text)
