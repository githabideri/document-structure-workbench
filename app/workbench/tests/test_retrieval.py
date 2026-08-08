"""Characterization tests for the extracted EvidenceRetriever boundary.

These assert the deterministic output (ordered passage/page IDs, scores and
selection reasons) of :class:`DeterministicLexicalRetriever` over fixed
fixtures so any accidental behavioral drift during the Milestone 2A structural
refactor is caught. The assertions intentionally encode the previous chat
retrieval semantics.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from workbench.models import (
    Collection, Document, Page, ProcessingJob, ProcessingPreset,
    ProjectMembership, SearchPassage, SourceDocument,
)
from workbench.retrieval import DeterministicLexicalRetriever
from workbench.search import normalize_text

User = get_user_model()


class DeterministicLexicalRetrieverTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("retrieval-user", password="pass")
        self.project = Collection.objects.create(name="Corpus", created_by=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="owner")
        self.retriever = DeterministicLexicalRetriever()

    def make_source(self, filename="doc.pdf", passages=(), project=None, preset_slug=None):
        project = project or self.project
        source = SourceDocument.objects.create(collection=project, filename=filename, uploaded_by=self.user)
        revision = Document.objects.create(collection=project, external_id=filename, filename=filename)
        preset = ProcessingPreset.objects.create(slug=preset_slug or f"preset-{filename}", name=filename)
        job = ProcessingJob.objects.create(source_document=source, preset=preset, state="completed")
        job.result_document = revision
        job.save(update_fields=["result_document"])
        source.active_document = revision
        source.save(update_fields=["active_document"])
        created = {}
        pages = {}
        for spec in passages:
            page_key = (revision.pk, spec["page"])
            if page_key not in pages:
                pages[page_key] = Page.objects.create(document=revision, page_number=spec["page"])
            page = pages[page_key]
            passage = SearchPassage.objects.create(
                project=project, source_document=source, processed_revision=revision,
                processing_job=job, page=page, passage_type="region",
                text=spec["text"], normalized_text=normalize_text(spec["text"]),
                ordinal=spec.get("ordinal", 1),
            )
            created[spec["key"]] = passage
        return source, revision, created

    def test_no_result_returns_empty(self):
        source, revision, _ = self.make_source("nohit.pdf", passages=[
            {"key": "p", "page": 1, "text": "The weather report is unremarkable today."},
        ])
        hits = self.retriever.search(
            query="restoration 1957", source_ids=[source.pk], project_ids=[self.project.pk],
        )
        self.assertEqual(hits, [])

    def test_exact_phrase_scores_bonus_and_ranks_first(self):
        source, revision, fixtures = self.make_source("phrase.pdf", passages=[
            {"key": "phrase", "page": 1, "text": "the restoration happened in 1957"},
            {"key": "scrambled", "page": 2, "text": "in 1957 the restoration happened"},
        ])
        hits = self.retriever.search(
            query="the restoration happened in 1957",
            source_ids=[source.pk], project_ids=[self.project.pk],
        )
        self.assertEqual([hit.passage.id for hit in hits], [fixtures["phrase"].id, fixtures["scrambled"].id])
        # tokens: the,restoration,happened,1957 (in is 2 chars and is excluded)
        self.assertEqual(hits[0].score, 14)  # 4 tokens + 10 exact-phrase bonus
        self.assertEqual(hits[1].score, 4)
        self.assertEqual(hits[0].reason, "query-match/page-coverage")

    def test_repeated_terms_affect_score(self):
        source, revision, fixtures = self.make_source("repeat.pdf", passages=[
            {"key": "single", "page": 1, "text": "a single 1957 mention"},
            {"key": "repeated", "page": 2, "text": "1957 1957 1957 repeated"},
        ])
        hits = self.retriever.search(
            query="1957", source_ids=[source.pk], project_ids=[self.project.pk],
        )
        self.assertEqual(hits[0].passage.id, fixtures["repeated"].id)
        self.assertEqual(hits[0].score, 13)  # 3 mentions + 10 single-token substring bonus
        self.assertEqual(hits[1].score, 11)  # 1 mention + 10 single-token substring bonus
        self.assertEqual(hits[1].passage.id, fixtures["single"].id)

    def test_multiple_matching_pages_are_page_diverse(self):
        source, revision, fixtures = self.make_source("pages.pdf", passages=[
            {"key": "p1a", "page": 1, "text": "restoration first", "ordinal": 1},
            {"key": "p1b", "page": 1, "text": "restoration restoration", "ordinal": 1},
            {"key": "p2", "page": 2, "text": "restoration page two", "ordinal": 1},
            {"key": "p3", "page": 3, "text": "restoration page three", "ordinal": 1},
        ])
        page_of = {fixtures[k].page_id for k in ("p1a", "p1b", "p2", "p3")}
        hits = self.retriever.search(
            query="restoration", source_ids=[source.pk], project_ids=[self.project.pk],
            limit=3,
        )
        # First pass: one best region per page, highest-scoring passage first.
        self.assertEqual(len(hits), 3)
        self.assertEqual([hit.passage.id for hit in hits][0], fixtures["p1b"].id)
        self.assertEqual({hit.passage.page_id for hit in hits}, page_of)  # all three pages covered
        self.assertTrue(all(hit.reason == "query-match/page-coverage" for hit in hits))

    def test_second_pass_fills_remaining_capacity(self):
        source, revision, fixtures = self.make_source("fill.pdf", passages=[
            {"key": "p1a", "page": 1, "text": "restoration first", "ordinal": 1},
            {"key": "p1b", "page": 1, "text": "restoration restoration", "ordinal": 1},
            {"key": "p2", "page": 2, "text": "restoration page two", "ordinal": 1},
            {"key": "p3", "page": 3, "text": "restoration page three", "ordinal": 1},
        ])
        hits = self.retriever.search(
            query="restoration", source_ids=[source.pk], project_ids=[self.project.pk],
            limit=4,
        )
        self.assertEqual(len(hits), 4)
        # The extra same-page passage is added back in score order as plain query-match.
        self.assertEqual(hits[-1].passage.id, fixtures["p1a"].id)
        self.assertEqual(hits[-1].reason, "query-match")
        self.assertEqual([h.reason for h in hits][:3], ["query-match/page-coverage"] * 3)

    def test_tie_break_by_ordinal_within_page(self):
        source, revision, fixtures = self.make_source("tie.pdf", passages=[
            {"key": "first", "page": 1, "text": "restoration", "ordinal": 1},
            {"key": "second", "page": 1, "text": "restoration", "ordinal": 9},
        ])
        hits = self.retriever.search(
            query="restoration", source_ids=[source.pk], project_ids=[self.project.pk],
        )
        self.assertEqual([hit.passage.id for hit in hits], [fixtures["first"].id, fixtures["second"].id])
        self.assertEqual(hits[0].reason, "query-match/page-coverage")
        self.assertEqual(hits[1].reason, "query-match")

    def test_revision_filtering(self):
        source = SourceDocument.objects.create(collection=self.project, filename="rev.pdf", uploaded_by=self.user)
        preset = ProcessingPreset.objects.create(slug="rev-preset", name="rev")
        def make_rev(rev_id, page_num, text):
            job = ProcessingJob.objects.create(source_document=source, preset=preset, state="completed")
            revision = Document.objects.create(collection=self.project, external_id=str(rev_id), filename="rev.pdf")
            job.result_document = revision
            job.save(update_fields=["result_document"])
            page = Page.objects.create(document=revision, page_number=page_num)
            return SearchPassage.objects.create(
                project=self.project, source_document=source, processed_revision=revision,
                processing_job=job, page=page, passage_type="region", text=text,
                normalized_text=normalize_text(text), ordinal=1,
            )
        rev1_pass = make_rev("r1", 1, "the restoration revision one")
        rev2_pass = make_rev("r2", 1, "the restoration revision two")
        # Without the filter, both revisions match.
        all_hits = self.retriever.search(query="restoration", source_ids=[source.pk], project_ids=[self.project.pk])
        self.assertEqual(len(all_hits), 2)
        # With the filter, only the frozen revision participates.
        filtered = self.retriever.search(
            query="restoration", source_ids=[source.pk], project_ids=[self.project.pk],
            revision_ids=[rev2_pass.processed_revision_id],
        )
        self.assertEqual([hit.passage.id for hit in filtered], [rev2_pass.id])

    def test_source_and_project_filtering(self):
        other = Collection.objects.create(name="Other", created_by=self.user)
        ProjectMembership.objects.create(project=other, user=self.user, role="owner")
        src_a, rev_a, fx_a = self.make_source("a.pdf", passages=[{"key": "a", "page": 1, "text": "the restoration doc a"}])
        src_b, rev_b, fx_b = self.make_source("b.pdf", passages=[{"key": "b", "page": 1, "text": "the restoration doc b"}], project=other)
        # Source filter restricts to that source alone.
        only_a = self.retriever.search(query="restoration", source_ids=[src_a.pk], project_ids=[self.project.pk])
        self.assertEqual([hit.passage.id for hit in only_a], [fx_a["a"].id])
        # Project filter restricts to that project's source.
        only_b = self.retriever.search(query="restoration", source_ids=[src_b.pk], project_ids=[other.pk])
        self.assertEqual([hit.passage.id for hit in only_b], [fx_b["b"].id])

    def test_attached_context_is_one_per_page_in_document_order(self):
        source, revision, fixtures = self.make_source("attach.pdf", passages=[
            {"key": "p1", "page": 1, "text": "page one text"},
            {"key": "p2a", "page": 2, "text": "page two a", "ordinal": 1},
            {"key": "p2b", "page": 2, "text": "page two b", "ordinal": 2},
        ])
        hits = self.retriever.attached_context(
            source_ids=[source.pk], project_ids=[self.project.pk],
        )
        # One region per page (p2b and p2a collapse to a single page context).
        self.assertEqual([hit.passage.id for hit in hits], [fixtures["p1"].id, fixtures["p2a"].id])
        self.assertTrue(all(hit.score == 0 for hit in hits))
        self.assertTrue(all(hit.method == "direct-attachment" for hit in hits))
        self.assertTrue(all(hit.reason == "attached-document/context" for hit in hits))
