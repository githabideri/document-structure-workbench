"""Tests for document/project text export (plain text + Markdown)."""
import io
import zipfile

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from workbench.export import DocumentExportService
from workbench.models import (
    ApiToken,
    AuditEvent,
    Collection,
    Document,
    Page,
    PageRegion,
    ProcessingJob,
    ProcessingPreset,
    ProjectMembership,
    RegionCorrection,
    SourceDocument,
)

User = get_user_model()


def build_revision(project, user, *, external_id, filename, title_text, body_text):
    """Create a completed source + revision + one page with a title and text region."""
    source = SourceDocument.objects.create(
        collection=project, filename=filename, uploaded_by=user, page_count=1,
    )
    preset = ProcessingPreset.objects.create(slug=external_id, name=external_id)
    job = ProcessingJob.objects.create(
        source_document=source, preset=preset, state="completed", processor="fixture",
    )
    revision = Document.objects.create(
        collection=project, external_id=external_id, filename=filename, page_count=1,
    )
    job.result_document = revision
    job.save(update_fields=["result_document"])
    source.active_document = revision
    source.save(update_fields=["active_document"])

    page = Page.objects.create(document=revision, page_number=1, width=100, height=200)
    title = PageRegion.objects.create(
        source_document=source, job=job, page=page, page_number=1, region_type="title",
        left=.1, top=.1, right=.9, bottom=.2, page_width=100, page_height=200, text=title_text,
    )
    body = PageRegion.objects.create(
        source_document=source, job=job, page=page, page_number=1, region_type="text",
        left=.1, top=.25, right=.9, bottom=.8, page_width=100, page_height=200, text=body_text,
    )
    return source, revision, title, body


class DocumentExportServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("curator", password="pass")
        self.project = Collection.objects.create(name="Export project", created_by=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="owner")
        self.source, self.revision, self.title, self.body = build_revision(
            self.project, self.user,
            external_id="doc-a", filename="letter-a.pdf",
            title_text="Original title", body_text="Original body",
        )

    def test_revision_data_applies_corrections_and_excludes_suppressed(self):
        RegionCorrection.objects.create(
            region=self.title, document=self.revision, created_by=self.user,
            operation="text", before={"text": "Original title"}, after={"text": "Corrected title"},
        )
        RegionCorrection.objects.create(
            region=self.body, document=self.revision, created_by=self.user,
            operation="suppress", before={}, after={},
        )
        data = DocumentExportService.revision_data(self.revision)
        page = data["pages"][0]
        types = [r["type"] for r in page["regions"]]
        self.assertEqual(types, ["title"])  # body suppressed, gone
        self.assertEqual(page["regions"][0]["text"], "Corrected title")

    def test_to_text_and_to_markdown_render_expected_content(self):
        md = DocumentExportService.to_markdown(DocumentExportService.revision_data(self.revision))
        self.assertIn("# letter-a.pdf", md)
        self.assertIn("### Original title", md)
        self.assertIn("Original body", md)
        self.assertIn("## Page 1", md)

        txt = DocumentExportService.to_text(DocumentExportService.revision_data(self.revision))
        self.assertIn("letter-a.pdf", txt)
        self.assertIn("Original title", txt)
        self.assertIn("Original body", txt)
        self.assertIn("Page 1", txt)

    def test_project_data_only_includes_active_revisions(self):
        # A second source without an active revision must be skipped.
        SourceDocument.objects.create(collection=self.project, filename="pending.pdf")
        data = DocumentExportService.project_data(self.project)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["revision_id"], self.revision.pk)

    def test_render_zip_contains_one_file_per_document_plus_index(self):
        build_revision(
            self.project, self.user, external_id="doc-b", filename="letter-b.pdf",
            title_text="Second title", body_text="Second body",
        )
        payload = DocumentExportService.render_zip(self.project, "md")
        archive = zipfile.ZipFile(io.BytesIO(payload))
        names = archive.namelist()
        self.assertIn("INDEX.md", names)
        self.assertEqual(sum(1 for n in names if n != "INDEX.md"), 2)
        joined = "\n".join(archive.read(n).decode() for n in names if n != "INDEX.md")
        self.assertIn("Original title", joined)
        self.assertIn("Second title", joined)


class ExportApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("api-user", password="pass")
        self.project = Collection.objects.create(name="API archive", created_by=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="owner")
        self.raw_token = "export-test-token"
        self.token = ApiToken.objects.create(
            user=self.user, name="test", token_prefix="export-",
            token_hash=ApiToken.hash_token(self.raw_token),
            scopes=["documents:read"],
        )
        self.source, self.revision, self.title, self.body = build_revision(
            self.project, self.user, external_id="doc-a", filename="letter-a.pdf",
            title_text="Original title", body_text="Original body",
        )

    def auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}

    def test_revision_export_requires_token_and_supports_both_formats(self):
        # No token -> 401
        self.assertEqual(
            self.client.get(reverse("api_revision_export", args=[self.source.pk, self.revision.pk])).status_code, 401
        )
        md = self.client.get(
            reverse("api_revision_export", args=[self.source.pk, self.revision.pk]), {"format": "md"}, **self.auth()
        )
        self.assertEqual(md.status_code, 200)
        self.assertEqual(md["Content-Type"], "text/markdown; charset=utf-8")
        self.assertIn("Original title", md.content.decode())
        self.assertIn('attachment; filename="doc-a.md"', md["Content-Disposition"])

        txt = self.client.get(
            reverse("api_revision_export", args=[self.source.pk, self.revision.pk]), {"format": "txt"}, **self.auth()
        )
        self.assertEqual(txt["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn("Original body", txt.content.decode())

    def test_revision_export_reflects_corrections_and_is_audited(self):
        RegionCorrection.objects.create(
            region=self.title, document=self.revision, created_by=self.user,
            operation="text", before={"text": "Original title"}, after={"text": "Audited title"},
        )
        response = self.client.get(
            reverse("api_revision_export", args=[self.source.pk, self.revision.pk]), {"format": "md"}, **self.auth()
        )
        self.assertIn("Audited title", response.content.decode())
        self.assertNotIn("Original title", response.content.decode())
        audit = AuditEvent.objects.get(object_id=str(self.revision.pk), event_type="document_exported")
        self.assertEqual(audit.after["format"], "md")

    def test_revision_export_rejects_unknown_format(self):
        response = self.client.get(
            reverse("api_revision_export", args=[self.source.pk, self.revision.pk]), {"format": "pdf"}, **self.auth()
        )
        self.assertEqual(response.status_code, 400)

    def test_revision_export_conceals_other_projects(self):
        other = Collection.objects.create(name="Private", created_by=self.user)
        other_source, other_revision, *_ = build_revision(
            other, self.user, external_id="secret", filename="secret.pdf",
            title_text="hidden", body_text="hidden body",
        )
        response = self.client.get(
            reverse("api_revision_export", args=[other_source.pk, other_revision.pk]), {"format": "md"}, **self.auth()
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(AuditEvent.objects.filter(object_id=str(other_revision.pk), event_type="document_exported").exists())

    def test_project_export_returns_audited_zip(self):
        build_revision(
            self.project, self.user, external_id="doc-b", filename="letter-b.pdf",
            title_text="Second title", body_text="Second body",
        )
        response = self.client.get(reverse("api_project_export", args=[self.project.pk]), {"format": "md"}, **self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        joined = "\n".join(archive.read(n).decode() for n in archive.namelist())
        self.assertIn("Original title", joined)
        self.assertIn("Second title", joined)
        audit = AuditEvent.objects.get(object_id=str(self.project.pk), event_type="project_exported")
        self.assertEqual(audit.after["format"], "md")

    def test_project_export_denies_non_members(self):
        response = self.client.get(reverse("api_project_export", args=[self.project.pk]), {"format": "md"}, **self.auth())
        self.assertEqual(response.status_code, 200)  # member OK
        outsider = User.objects.create_user("outsider", password="pass")
        raw = "outsider-token"
        ApiToken.objects.create(
            user=outsider, name="out", token_prefix="out-",
            token_hash=ApiToken.hash_token(raw), scopes=["documents:read"],
        )
        denied = self.client.get(
            reverse("api_project_export", args=[self.project.pk]), {"format": "md"},
            HTTP_AUTHORIZATION=f"Bearer {raw}",
        )
        self.assertEqual(denied.status_code, 403)


class ExportViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("member", password="pass")
        self.project = Collection.objects.create(name="Web project", created_by=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="owner")
        self.source, self.revision, self.title, self.body = build_revision(
            self.project, self.user, external_id="doc-a", filename="letter-a.pdf",
            title_text="Original title", body_text="Original body",
        )

    def test_revision_export_requires_login(self):
        self.assertEqual(
            self.client.get(reverse("export_revision", args=[self.revision.pk])).status_code, 302
        )

    def test_member_can_export_revision_and_project(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("export_revision", args=[self.revision.pk]), {"format": "txt"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Original body", response.content.decode())
        self.assertTrue(AuditEvent.objects.filter(event_type="document_exported").exists())

        zip_response = self.client.get(reverse("export_project", args=[self.project.pk]), {"format": "md"})
        self.assertEqual(zip_response.status_code, 200)
        self.assertEqual(zip_response["Content-Type"], "application/zip")

    def test_non_member_is_denied(self):
        outsider = User.objects.create_user("outsider", password="pass")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(reverse("export_revision", args=[self.revision.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("export_project", args=[self.project.pk])).status_code, 403)
