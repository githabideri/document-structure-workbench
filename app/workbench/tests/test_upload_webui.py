"""WebUI upload endpoint contract for the drag-and-drop workflow.

The drop-zone JS (``upload-workspace.js``) funnels a dropped file into the same
``<input type="file" name="file">`` the click-to-select path uses, then the
normal explicit submit posts it. These tests lock that backend contract so a
dropped file (which becomes ``request.FILES["file"]`` on submit) creates a
processing job and follows to the job status page, while unsupported/missing
files are rejected with a visible message. They do not change ingestion,
revision, or processing semantics.
"""
import os
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from workbench.models import Collection, ProcessingJob, ProcessingPreset, ProjectMembership

User = get_user_model()


def _make_pdf_content():
    return b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >> endobj
4 0 obj << /Length 11 >> stream
BT /F1 12 Tf (a) Tj ET
endstream endobj
xref
0 5
0000000000 65535 f
trailer << /Size 5 /Root 1 0 R >>
startxref
1
%%EOF"""


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class DocumentUploadWebUITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("uploader", password="pass")
        self.collection = Collection.objects.create(name="Upload Project", created_by=self.user)
        ProjectMembership.objects.create(project=self.collection, user=self.user, role="editor")
        self.preset = ProcessingPreset.objects.create(slug="webui-test-extraction", name="WebUI Test")
        self.client.login(username="uploader", password="pass")

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_upload_view_renders_drop_zone_markup(self):
        response = self.client.get(reverse("document_new"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('id="file-drop"', content)
        self.assertIn('id="file-input"', content)
        # Multi-file: the input accepts several files and the JS mounts a
        # per-file list + summary below the drop zone.
        self.assertIn('multiple', content)
        self.assertIn('id="file-list"', content)
        self.assertIn('id="file-summary"', content)
        # The configured size cap is surfaced to the UI from settings.
        self.assertIn('data-max-bytes=', content)
        self.assertIn('data-max-files=', content)

    @staticmethod
    def _valid_image(name="FV_0100.jpg", fmt="JPEG"):
        from io import BytesIO
        from PIL import Image
        buffer = BytesIO()
        Image.new("RGB", (64, 64), "white").save(buffer, format=fmt)
        buffer.seek(0)
        return SimpleUploadedFile(name, buffer.read(), content_type={"JPEG": "image/jpeg", "PNG": "image/png"}[fmt])

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_multiple_files_each_create_a_job_and_redirect_to_project(self):
        """A multi-file post creates one queued job per accepted file and lands
        on the project view; the good files still upload when one is bad."""
        good_a = self._valid_image(name="a.jpg")
        good_b = self._valid_image(name="b.png", fmt="PNG")
        bad = SimpleUploadedFile("malware.exe", b"MZ...", content_type="application/x-msdownload")
        response = self.client.post(reverse("document_new"), {
            "project": self.collection.pk,
            "file": [good_a, good_b, bad],
            "preset": self.preset.slug,
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        jobs = ProcessingJob.objects.filter(source_document__collection=self.collection).order_by("pk")
        self.assertEqual(jobs.count(), 2)
        self.assertTrue(all(job.state == "queued" for job in jobs))
        # Landed on the project detail page (multi-file landing), with the
        # per-file failure surfaced as a visible message.
        self.assertRedirects(response, reverse("collection_detail", args=[self.collection.pk]))
        self.assertIn(b"malware.exe", response.content)
        self.assertIn(b"Supported files are PDF, JPG, PNG, and TIFF.", response.content)

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_single_file_still_redirects_to_its_job_status(self):
        """A single-file upload keeps the original UX: follow to job status."""
        img = self._valid_image(name="solo.jpg")
        response = self.client.post(reverse("document_new"), {
            "project": self.collection.pk,
            "file": img,
            "preset": self.preset.slug,
        })
        job = ProcessingJob.objects.get(source_document__collection=self.collection)
        self.assertRedirects(response, reverse("job_status", args=[job.pk]))
        self.assertEqual(job.state, "queued")

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_single_file_xhr_endpoint_returns_job_urls(self):
        """The drop zone posts one file at a time to the XHR endpoint; it returns
        JSON with the job and redirect URLs and creates a queued job."""
        img = self._valid_image(name="xhr.jpg")
        response = self.client.post(
            reverse("document_upload_file"),
            {"project": self.collection.pk, "file": img, "preset": self.preset.slug},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        job = ProcessingJob.objects.get(source_document__collection=self.collection)
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["job_id"], job.pk)
        self.assertEqual(body["job_status_url"], reverse("job_status", args=[job.pk]))
        self.assertEqual(body["collection_url"], reverse("collection_detail", args=[self.collection.pk]))

    def test_xhr_endpoint_requires_edit_access(self):
        viewer = User.objects.create_user(username="viewer", password="pw")
        from workbench.models import ProjectMembership
        ProjectMembership.objects.create(project=self.collection, user=viewer, role="viewer")
        self.client.login(username="viewer", password="pw")
        response = self.client.post(
            reverse("document_upload_file"),
            {"project": self.collection.pk, "file": self._valid_image(), "preset": self.preset.slug},
        )
        self.assertEqual(response.status_code, 403)

    def test_xhr_endpoint_rejects_unsupported_file_with_json_error(self):
        bad = SimpleUploadedFile("x.exe", b"MZ", content_type="application/x-msdownload")
        response = self.client.post(
            reverse("document_upload_file"),
            {"project": self.collection.pk, "file": bad, "preset": self.preset.slug},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertEqual(ProcessingJob.objects.filter(source_document__collection=self.collection).count(), 0)

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_submit_after_selection_creates_job_and_follows_status(self):
        """A dropped file lands in the file input; the normal submit (the same
        path the drop uses) posts it, creates a queued job, and redirects."""
        img = self._valid_image()
        response = self.client.post(reverse("document_new"), {
            "project": self.collection.pk,
            "file": img,
            "preset": self.preset.slug,
        })
        self.assertEqual(response.status_code, 302)
        job = ProcessingJob.objects.filter(source_document__collection=self.collection).latest("pk")
        self.assertEqual(job.state, "queued")
        self.assertEqual(response.url, reverse("job_status", kwargs={"job_id": job.pk}))
        self.assertEqual(job.source_document.filename, "FV_0100.jpg")

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_unsupported_drop_is_rejected_with_message(self):
        bad = SimpleUploadedFile("malware.exe", b"MZ...", content_type="application/x-msdownload")
        response = self.client.post(reverse("document_new"), {
            "project": self.collection.pk,
            "file": bad,
            "preset": self.preset.slug,
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        # A visible error is shown and no job is created, so an invalid drop
        # can never silently succeed.
        self.assertIn(b"Supported files are PDF, JPG, PNG, and TIFF.", response.content)
        self.assertEqual(ProcessingJob.objects.filter(source_document__collection=self.collection).count(), 0)

    @override_settings(ARTIFACTS_BASE_DIR=tempfile.mkdtemp())
    def test_missing_file_shows_message(self):
        response = self.client.post(reverse("document_new"), {
            "project": self.collection.pk,
            "preset": self.preset.slug,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose a PDF or image to upload", response.content)
        self.assertEqual(ProcessingJob.objects.filter(source_document__collection=self.collection).count(), 0)

    def test_invalid_edit_project_is_rejected(self):
        # A second editable project makes the project choice meaningful so the
        # automatic single-project default does not mask an invalid selection.
        second = Collection.objects.create(name="Second", created_by=self.user)
        ProjectMembership.objects.create(project=second, user=self.user, role="editor")
        img = self._valid_image()
        response = self.client.post(reverse("document_new"), {
            "project": "999999", "file": img, "preset": self.preset.slug,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose a project you can edit", response.content)
        self.assertEqual(ProcessingJob.objects.count(), 0)
