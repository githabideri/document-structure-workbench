"""Unit tests for the conservative fuzzy/lexical retriever (Milestone 2B).

Focus on the tolerant-matching primitives (punctuation, hyphenation, Unicode,
small OCR error, diacritic variation) and on the ranking guarantees that keep
fuzzy retrieval conservative: exact matches outrank fuzzy matches, unrelated
fuzzy candidates never beat strong lexical evidence, short/common tokens never
produce fuzzy noise, and strict source/project/revision filtering is preserved.
The deterministic retriever itself stays frozen and is covered unchanged by
``test_retrieval.py``.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from workbench.models import (
    Collection, Document, Page, ProcessingJob, ProcessingPreset,
    ProjectMembership, SearchPassage, SourceDocument,
)
from workbench.retrieval import (
    DeterministicLexicalRetriever, FuzzyLexicalRetriever,
    alpha, fuzzy_token_match, get_retriever, lexical_tokens,
)
from workbench.search import normalize_text

User = get_user_model()


class FuzzyPrimitiveTests(TestCase):
    def test_alpha_strips_punctuation_and_hyphens(self):
        self.assertEqual(alpha("Gemeinde-verordnung, Freistadt!!"), "gemeindeverordnungfreistadt")
        # A line-break hyphen break "Reise- verordnung" collapses to "reiseverordnung".
        self.assertEqual(alpha("die Reise- verordnung"), "diereiseverordnung")

    def test_lexical_tokens_are_alnum_runs_len3plus(self):
        self.assertEqual(lexical_tokens(normalize_text("Reise-verordnung 1957, x yz")), ["reise", "verordnung", "1957"])
        # Unicode letters (umlauts) are kept as tokens.
        self.assertEqual(lexical_tokens(normalize_text("Schäffer in Österreich")), ["schäffer", "österreich"])

    def test_ocr_character_error_matches(self):
        self.assertTrue(fuzzy_token_match("schachermair", "schachermayr"))
        self.assertTrue(fuzzy_token_match("fassling", "fassling"))
        # Within a bounded distance only.
        self.assertFalse(fuzzy_token_match("schachermair", "schachermayerhofer"))

    def test_diacritic_variation_matches(self):
        self.assertTrue(fuzzy_token_match("chaeffer".replace("ae", "ä"), "schaffer"))

    def test_short_common_tokens_never_fuzzy_match(self):
        self.assertFalse(fuzzy_token_match("und", "xund"))
        self.assertFalse(fuzzy_token_match("jahr", "jahren"))
        self.assertFalse(fuzzy_token_match("", ""))
        # Unrelated long tokens are not matched either.
        self.assertFalse(fuzzy_token_match("gemeinde", "gewerbesteuer"))


class FuzzyLexicalRetrieverSetupMixin:
    def setUp(self):
        self.user = User.objects.create_user("fuzzy-user", password="pass")
        self.project = Collection.objects.create(name="Corpus", created_by=self.user)
        ProjectMembership.objects.create(project=self.project, user=self.user, role="owner")
        self.retriever = FuzzyLexicalRetriever()

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
        pages = {}
        created = {}
        for spec in passages:
            key = (revision.pk, spec["page"])
            if key not in pages:
                pages[key] = Page.objects.create(document=revision, page_number=spec["page"])
            page = pages[key]
            passage = SearchPassage.objects.create(
                project=project, source_document=source, processed_revision=revision,
                processing_job=job, page=page, passage_type=spec.get("type", "region"),
                text=spec["text"], normalized_text=normalize_text(spec["text"]),
                ordinal=spec.get("ordinal", 1),
                archival_identifier=spec.get("archival_identifier", ""),
                heading_context=spec.get("heading_context", ""),
            )
            created[spec["key"]] = passage
        return source, revision, created

    def search(self, query, source_ids, project_ids, revision_ids=None, limit=24):
        return self.retriever.search(
            query=query, source_ids=source_ids, project_ids=project_ids,
            revision_ids=revision_ids, limit=limit,
        )


class FuzzyLexicalRetrieverTests(FuzzyLexicalRetrieverSetupMixin, TestCase):
    def test_ocr_damaged_personal_name_found_when_deterministic_misses(self):
        source, revision, fx = self.make_source("name.pdf", passages=[
            {"key": "mayr", "page": 1, "text": "An den Gemeindevorsteher Schachermayr zu Freistadt"},
        ])
        deterministic = DeterministicLexicalRetriever().search(
            query="Schachermair", source_ids=[source.pk], project_ids=[self.project.pk],
        )
        self.assertEqual(deterministic, [])
        hits = self.search("Schachermair", [source.pk], [self.project.pk])
        self.assertEqual([hit.passage.id for hit in hits], [fx["mayr"].id])
        self.assertEqual(hits[0].reason, "fuzzy-lexical/fuzzy-token/page-coverage")
        self.assertGreater(hits[0].score, 0)

    def test_punctuation_and_hyphen_tolerated_phrase(self):
        source, revision, fx = self.make_source("hyphen.pdf", passages=[
            {"key": "reise", "page": 1, "text": "Die Reise- verordnung über Freistadt"},
        ])
        hits = self.search("Reiseverordnung über Freistadt", [source.pk], [self.project.pk])
        self.assertEqual([hit.passage.id for hit in hits], [fx["reise"].id])
        self.assertTrue(hits[0].reason.startswith("fuzzy-lexical/punctuation-tolerated-phrase"))

    def test_unicode_diacritic_variant_found(self):
        source, revision, fx = self.make_source("umlaut.pdf", passages=[
            {"key": "schaffer", "page": 1, "text": "der Schaffer aus Linz lieferte die Rechnung"},
        ])
        hits = self.search("Schäffer", [source.pk], [self.project.pk])
        self.assertEqual([hit.passage.id for hit in hits], [fx["schaffer"].id])

    def test_exact_match_outranks_fuzzy_match(self):
        source, revision, fx = self.make_source("exactvs.pdf", passages=[
            {"key": "fuzzy", "page": 1, "text": "genannt wurde der Name Schachermayr am Rand"},
            {"key": "exact", "page": 2, "text": "Die Liste führt Schachermair mit vollem Namen"},
        ])
        hits = self.search("Schachermair", [source.pk], [self.project.pk])
        self.assertEqual(hits[0].passage.id, fx["exact"].id)
        self.assertEqual(hits[1].passage.id, fx["fuzzy"].id)
        self.assertGreater(hits[0].score, hits[1].score)

    def test_unrelated_fuzzy_candidate_does_not_outrank_strong_lexical(self):
        source, revision, fx = self.make_source("strong.pdf", passages=[
            {"key": "strong", "page": 1, "text": "Die Restaurierung des Kirchturms erfolgte 1957 nach Restaurierung Beschluss"},
            {"key": "weakfuzzy", "page": 2, "text": "Eine vage Restaurirung Notiz ohne genaue Angabe"},
        ])
        hits = self.search("Restaurierung 1957", [source.pk], [self.project.pk])
        self.assertEqual(hits[0].passage.id, fx["strong"].id)
        self.assertGreater(hits[0].score, hits[1].score)

    def test_short_common_terms_do_not_explode_results(self):
        source, revision, fx = self.make_source("short.pdf", passages=[
            {"key": "hasund", "page": 1, "text": "Rundgang durch den oberen Marktplatz"},
            {"key": "noun", "page": 2, "text": "Der Hund und die Katze"},
        ])
        # "und" (len 3) is below the fuzzy minimum, so fuzzy must not release any
        # candidate that deterministic lexical would not; result sets are identical.
        fuzzy_ids = {h.passage.id for h in self.search("und", [source.pk], [self.project.pk])}
        det_ids = {h.passage.id for h in DeterministicLexicalRetriever().search(
            query="und", source_ids=[source.pk], project_ids=[self.project.pk])}
        self.assertEqual(fuzzy_ids, det_ids)
        # Both are substring matches on "und"; identify the word-boundary one is not
        # hand-picked here, just assert fuzzy never adds candidates via short tokens.
        self.assertLessEqual(len(fuzzy_ids), 2)

    def test_deterministic_tie_break_by_ordinal(self):
        source, revision, fx = self.make_source("tie.pdf", passages=[
            {"key": "first", "page": 1, "text": "restaurierung", "ordinal": 1},
            {"key": "second", "page": 1, "text": "restaurierung", "ordinal": 9},
        ])
        hits = self.search("restaurierung", [source.pk], [self.project.pk])
        self.assertEqual([h.passage.id for h in hits], [fx["first"].id, fx["second"].id])
        self.assertEqual(hits[0].reason, "fuzzy-lexical/exact-phrase/page-coverage")

    def test_page_diversity(self):
        source, revision, fx = self.make_source("pages.pdf", passages=[
            {"key": "p1", "page": 1, "text": "restaurierung erste Seite"},
            {"key": "p2", "page": 2, "text": "restaurierung zweite Seite"},
            {"key": "p3", "page": 3, "text": "restaurierung dritte Seite"},
        ])
        hits = self.search("restaurierung", [source.pk], [self.project.pk], limit=2)
        self.assertEqual(len([h for h in hits]), 2)
        self.assertNotEqual(hits[0].passage.page_id, hits[1].passage.page_id)

    def test_revision_filtering_strict(self):
        source = SourceDocument.objects.create(collection=self.project, filename="rev.pdf", uploaded_by=self.user)
        preset = ProcessingPreset.objects.create(slug="rev-fuzzy", name="rev")
        def make_rev(rid, text):
            job = ProcessingJob.objects.create(source_document=source, preset=preset, state="completed")
            revision = Document.objects.create(collection=self.project, external_id=rid, filename="rev.pdf")
            job.result_document = revision
            job.save(update_fields=["result_document"])
            page = Page.objects.create(document=revision, page_number=1)
            return SearchPassage.objects.create(
                project=self.project, source_document=source, processed_revision=revision,
                processing_job=job, page=page, passage_type="region", text=text,
                normalized_text=normalize_text(text), ordinal=1,
            )
        rev1 = make_rev("r1", "restaurierung revision eins")
        rev2 = make_rev("r2", "restaurierung revision zwei")
        self.assertEqual(len(self.search("restaurierung", [source.pk], [self.project.pk])), 2)
        filtered = self.search(
            "restaurierung", [source.pk], [self.project.pk],
            revision_ids=[rev2.processed_revision_id],
        )
        self.assertEqual([h.passage.id for h in filtered], [rev2.id])

    def test_source_and_project_filtering_strict(self):
        other = Collection.objects.create(name="Other", created_by=self.user)
        ProjectMembership.objects.create(project=other, user=self.user, role="owner")
        src_a, _, fx_a = self.make_source("a.pdf", passages=[{"key": "a", "page": 1, "text": "restaurierung im Projekt A"}])
        src_b, _, fx_b = self.make_source("b.pdf", passages=[{"key": "b", "page": 1, "text": "restaurierung im Projekt B"}], project=other)
        only_a = self.search("restaurierung", [src_a.pk], [self.project.pk])
        self.assertEqual([h.passage.id for h in only_a], [fx_a["a"].id])
        only_b = self.search("restaurierung", [src_b.pk], [other.pk])
        self.assertEqual([h.passage.id for h in only_b], [fx_b["b"].id])

    def test_no_result_returns_empty(self):
        source, _, _ = self.make_source("nohit.pdf", passages=[{"key": "p", "page": 1, "text": "Das Wetter war heute sonnig."}])
        self.assertEqual(self.search("Bürgermeister 1820", [source.pk], [self.project.pk]), [])

    def test_archival_identifier_boost(self):
        source, revision, fx = self.make_source("arch.pdf", passages=[
            {"key": "id", "page": 1, "text": "Rundschreiben an alle Einwohner", "archival_identifier": "BV_0042/1946"},
            {"key": "other", "page": 2, "text": "Rundschreiben an alle Einwohner"},
        ])
        hits = self.search("BV 0042", [source.pk], [self.project.pk])
        self.assertEqual(hits[0].passage.id, fx["id"].id)
        self.assertTrue(hits[0].reason.startswith("fuzzy-lexical/archival-id"))

    def test_attached_context_delegates_to_direct_attachment(self):
        source, revision, fx = self.make_source("attach.pdf", passages=[
            {"key": "p1", "page": 1, "text": "page one"},
            {"key": "p2a", "page": 2, "text": "page two a", "ordinal": 1},
            {"key": "p2b", "page": 2, "text": "page two b", "ordinal": 2},
        ])
        hits = self.retriever.attached_context(source_ids=[source.pk], project_ids=[self.project.pk])
        self.assertEqual([h.passage.id for h in hits], [fx["p1"].id, fx["p2a"].id])
        self.assertTrue(all(h.method == "direct-attachment" for h in hits))
        self.assertTrue(all(h.reason == "attached-document/context" for h in hits))

    def test_stats_recorded_and_scored_passages_bounded(self):
        source, _, _ = self.make_source("stats.pdf", passages=[{"key": "a", "page": 1, "text": "restaurierung hier"}])
        self.search("restaurierung", [source.pk], [self.project.pk])
        stats = self.retriever.last_stats
        self.assertEqual(stats["passages_scanned"], 1)
        self.assertEqual(stats["hits_returned"], 1)
        self.assertGreaterEqual(stats["retrieval_ms"], 0)


class RetrieverFactoryTests(TestCase):
    @override_settings(DSW_CHAT_RETRIEVER="deterministic_lexical")
    def test_default_is_deterministic(self):
        self.assertEqual(get_retriever().name, "deterministic_lexical")

    @override_settings(DSW_CHAT_RETRIEVER="fuzzy_lexical")
    def test_fuzzy_selectable(self):
        self.assertEqual(get_retriever().name, "fuzzy_lexical")

    def test_invalid_config_falls_back_to_deterministic(self):
        self.assertEqual(get_retriever("not-a-retriever").name, "deterministic_lexical")
        self.assertEqual(get_retriever("").name, "deterministic_lexical")

    def test_explicit_names(self):
        self.assertEqual(get_retriever("fuzzy_lexical").name, "fuzzy_lexical")
        self.assertEqual(get_retriever("deterministic_lexical").name, "deterministic_lexical")
