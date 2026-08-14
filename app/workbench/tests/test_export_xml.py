"""Tests for PAGE-XML and TEI export (ADR 0004)."""
import io
import zipfile
from xml.dom import minidom

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from workbench.export import DocumentExportService
from workbench.models import (
    ApiToken,
    Collection,
    Document,
    Page,
    PageRegion,
    ProcessingJob,
    ProcessingPreset,
    ProjectMembership,
    SourceDocument,
)

User = get_user_model()


def build_revision(project, user, *, external_id, filename, pages_spec):
    """Create a completed source + revision with the given pages/regions.

    ``pages_spec`` is a list of (page_number, width, height) tuples; each page
    gets a title region and a text region with marker-rich text.
    """
    source = SourceDocument.objects.create(
        collection=project, filename=filename, uploaded_by=user, page_count=len(pages_spec),
    )
    preset = ProcessingPreset.objects.create(slug=external_id, name=external_id)
    job = ProcessingJob.objects.create(
        source_document=source, preset=preset, state="completed", processor="fixture",
    )
    revision = Document.objects.create(
        collection=project, external_id=external_id, filename=filename, page_count=len(pages_spec),
    )
    job.result_document = revision
    job.save(update_fields=["result_document"])
    source.active_document = revision
    source.save(update_fields=["active_document"])

    for page_number, width, height in pages_spec:
        page = Page.objects.create(
            document=revision, page_number=page_number, width=width, height=height,
        )
        PageRegion.objects.create(
            source_document=source, job=job, page=page, page_number=page_number,
            region_type="title", left=.1, top=.05, right=.9, bottom=.15,
            page_width=width, page_height=height, text="Kopfzeile",
        )
        PageRegion.objects.create(
            source_document=source, job=job, page=page, page_number=page_number,
            region_type="text", left=.1, top=.2, right=.9, bottom=.8,
            page_width=width, page_height=height,
            text="An d.[omi]no Wort[?] dann [illegible] Ende",
        )
    return source, revision


class PageXmlExportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("curator", password="pass")
        self.project = Collection.objects.create(name="XML project", created_by=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="owner")
        _, self.revision = build_revision(
            self.project, self.user,
            external_id="doc-xml", filename="letter.pdf",
            pages_spec=[(1, 100, 200)],
        )
        self.data = DocumentExportService.revision_data(self.revision)

    def test_single_page_renders_one_valid_pc_gts(self):
        xml = DocumentExportService.to_page_xml(self.data, self.data["pages"][0])
        dom = minidom.parseString(xml)
        self.assertEqual(dom.documentElement.tagName, "PcGts")
        self.assertEqual(
            dom.documentElement.namespaceURI,
            "http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15",
        )
        pages = dom.getElementsByTagName("Page")
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].getAttribute("imageWidth"), "100")
        self.assertEqual(pages[0].getAttribute("imageHeight"), "200")
        # Title maps to a heading-typed TextRegion with integer pixel coords.
        regions = dom.getElementsByTagName("TextRegion")
        self.assertEqual(regions[0].getAttribute("type"), "heading")
        points = regions[0].getElementsByTagName("Coords")[0].getAttribute("points")
        self.assertEqual(points, "10,10 90,10 90,30 10,30")
        unicode_nodes = regions[1].getElementsByTagName("Unicode")
        self.assertEqual(
            unicode_nodes[0].firstChild.nodeValue,
            "An d.[omi]no Wort[?] dann [illegible] Ende",  # markers stay literal
        )

    def test_page_xml_payload_single_page_is_plain_xml(self):
        payload, content_type, filename = DocumentExportService.page_xml_payload(self.data)
        minidom.parseString(payload)  # well-formed
        self.assertEqual(content_type, "application/xml; charset=utf-8")
        self.assertEqual(filename, "doc-xml.xml")

    def test_page_xml_payload_multi_page_is_zip_of_pages(self):
        _, revision2 = build_revision(
            self.project, self.user,
            external_id="doc-xml-2", filename="letter2.pdf",
            pages_spec=[(1, 100, 200), (2, 100, 200)],
        )
        data2 = DocumentExportService.revision_data(revision2)
        payload, content_type, filename = DocumentExportService.page_xml_payload(data2)
        self.assertEqual(content_type, "application/zip")
        self.assertEqual(filename, "doc-xml-2-pages.zip")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.namelist(), ["page-0001.xml", "page-0002.xml"])
            for name in archive.namelist():
                minidom.parseString(archive.read(name))  # each page well-formed


class TeiExportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("curator", password="pass")
        self.project = Collection.objects.create(name="TEI project", created_by=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="owner")
        _, self.revision = build_revision(
            self.project, self.user,
            external_id="doc-tei", filename="letter.pdf",
            pages_spec=[(1, 100, 200), (2, 100, 200)],
        )
        self.data = DocumentExportService.revision_data(self.revision)

    def test_tei_document_is_well_formed_with_header_and_pages(self):
        tei = DocumentExportService.to_tei(self.data)
        dom = minidom.parseString(tei)
        self.assertEqual(
            dom.documentElement.namespaceURI, "http://www.tei-c.org/ns/1.0",
        )
        self.assertEqual(len(dom.getElementsByTagName("teiHeader")), 1)
        page_divs = dom.getElementsByTagName("div")
        self.assertEqual(len(page_divs), 2)
        self.assertEqual(page_divs[0].getAttribute("n"), "1")
        self.assertEqual(page_divs[1].getAttribute("n"), "2")

    def test_editorial_markers_convert_to_tei_elements(self):
        tei = DocumentExportService.to_tei(self.data)
        dom = minidom.parseString(tei)
        unclear = dom.getElementsByTagName("unclear")
        self.assertEqual(len(unclear), 2)  # one per page
        self.assertEqual(unclear[0].firstChild.nodeValue, "Wort")
        gaps = dom.getElementsByTagName("gap")
        self.assertEqual(len(gaps), 2)
        self.assertEqual(gaps[0].getAttribute("reason"), "illegible")
        choices = dom.getElementsByTagName("choice")
        self.assertEqual(len(choices), 2)
        self.assertEqual(choices[0].getElementsByTagName("abbr")[0].firstChild.nodeValue, "d.")
        self.assertEqual(choices[0].getElementsByTagName("expan")[0].firstChild.nodeValue, "omi")

    def test_standalone_uncertain_marker_renders_empty_unclear(self):
        data = {"pages": [{"page_number": 1, "image_width": 10, "image_height": 10, "regions": [
            {"type": "text", "text": "dann [?] so", "left": 0, "top": 0, "right": 1, "bottom": 1},
        ]}]}
        tei = DocumentExportService.to_tei({**self._minimal_data(), "pages": data["pages"]})
        dom = minidom.parseString(tei)
        unclear = dom.getElementsByTagName("unclear")
        self.assertEqual(len(unclear), 1)
        self.assertEqual(unclear[0].getAttribute("reason"), "uncertain")

    def test_xml_entities_are_escaped(self):
        data = {"pages": [{"page_number": 1, "image_width": 10, "image_height": 10, "regions": [
            {"type": "text", "text": "a < b & c", "left": 0, "top": 0, "right": 1, "bottom": 1},
        ]}]}
        tei = DocumentExportService.to_tei({**self._minimal_data(), "pages": data["pages"]})
        dom = minidom.parseString(tei)
        paragraphs = dom.getElementsByTagName("p")
        self.assertTrue(any(p.firstChild.nodeValue == "a < b & c" for p in paragraphs))

    def _minimal_data(self):
        return {
            "source_filename": "x.pdf", "revision_id": 1, "external_id": "x",
            "page_count": 1, "created_at": "2026-01-01T00:00:00+00:00", "processor": "fixture",
        }


class ExportViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("curator", password="pass")
        self.client.login(username="curator", password="pass")
        self.project = Collection.objects.create(name="Views project", created_by=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="owner")
        _, self.revision = build_revision(
            self.project, self.user,
            external_id="doc-v", filename="letter.pdf", pages_spec=[(1, 100, 200)],
        )

    def test_revision_export_tei(self):
        response = self.client.get(reverse("export_revision", args=[self.revision.pk]), {"format": "tei"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/xml; charset=utf-8")
        self.assertIn(".tei.xml", response["Content-Disposition"])
        minidom.parseString(response.content.decode())

    def test_revision_export_page_xml_single_page(self):
        response = self.client.get(reverse("export_revision", args=[self.revision.pk]), {"format": "xml"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/xml; charset=utf-8")
        minidom.parseString(response.content.decode())

    def test_unknown_format_is_rejected(self):
        response = self.client.get(reverse("export_revision", args=[self.revision.pk]), {"format": "rtf"})
        self.assertEqual(response.status_code, 400)

    def test_project_export_page_xml_zip_contains_per_page_files(self):
        response = self.client.get(reverse("export_project", args=[self.project.pk]), {"format": "xml"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            self.assertIn("01-doc-v/page-0001.xml", names)
            minidom.parseString(archive.read("01-doc-v/page-0001.xml"))

    def test_xml_formats_reject_single_file_bundle(self):
        for fmt in ("xml", "tei"):
            response = self.client.get(
                reverse("export_project", args=[self.project.pk]), {"format": fmt, "bundle": "single"},
            )
            self.assertEqual(response.status_code, 400, msg=fmt)


class ExportApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("api-user", password="pass")
        self.project = Collection.objects.create(name="API XML", created_by=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="owner")
        self.raw_token = "api-test-token"
        self.token = ApiToken.objects.create(
            user=self.user, name="test", token_prefix="api-test",
            token_hash=ApiToken.hash_token(self.raw_token), scopes=["documents:read"],
        )
        self.source, self.revision = build_revision(
            self.project, self.user,
            external_id="doc-api", filename="letter.pdf", pages_spec=[(1, 100, 200)],
        )

    def auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}

    def test_api_revision_export_tei_and_page_xml(self):
        url = "/api/v1/documents/{}/revisions/{}/export/"
        for fmt, expected in (("tei", "application/xml; charset=utf-8"), ("xml", "application/xml; charset=utf-8")):
            response = self.client.get(
                url.format(self.source.pk, self.revision.pk), {"format": fmt}, **self.auth(),
            )
            self.assertEqual(response.status_code, 200, msg=fmt)
            self.assertIn(expected, response["Content-Type"])
            minidom.parseString(response.content.decode())

    def test_api_rejects_xml_single_file_bundle(self):
        response = self.client.get(
            f"/api/v1/projects/{self.project.pk}/export/",
            {"format": "tei", "bundle": "single"}, **self.auth(),
        )
        self.assertEqual(response.status_code, 400)
