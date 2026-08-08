"""Chat evidence retrieval boundary.

Milestone 2A: extract passage selection/ranking out of chat orchestration into
a small ``EvidenceRetriever`` interface without intentionally changing
retrieval behavior. The concrete implementation here reproduces the previous
deterministic lexical algorithm exactly.

``DeterministicLexicalRetriever`` is the reference/fallback implementation and
is intentionally kept free of embeddings, fuzzy matching and DB full-text
search. Those belong to later milestones so retrieval-quality changes can be
evaluated independently from this structural refactor.
"""
import re
from dataclasses import dataclass

from .models import SearchPassage
from .search import normalize_text


@dataclass
class EvidenceHit:
    """One selected evidence location.

    ``passage`` is the indexed immutable passage; ``score``, ``method`` and
    ``reason`` are provenance that the caller persists on the evidence item or
    into diagnostics. ``method`` identifies the retrieval route (e.g.
    ``direct-attachment`` or ``deterministic_lexical``).
    """
    passage: SearchPassage
    score: float
    method: str
    reason: str


class EvidenceRetriever:
    """Interface for selecting chat evidence from immutable search passages."""

    name = "base"

    def attached_context(self, *, source_ids, project_ids, revision_ids=None, limit=24):
        """Select explicit user-attached, full-page context items."""
        raise NotImplementedError

    def search(self, *, query, source_ids, project_ids, revision_ids=None, limit=24):
        """Search the scope and return ranked, page-diverse hits."""
        raise NotImplementedError


class DeterministicLexicalRetriever(EvidenceRetriever):
    """Current deterministic lexical retriever (behavior-preserving extraction).

    Reproduces exactly the previous chat retrieval:

    * query tokens are ``\\w-`` runs of length >= 3 over ``normalize_text(query)``;
    * every eligible passage is scored by the count of each query token in its
      normalized text, plus a bonus of 10 when the normalized whole question is
      a substring of the passage text;
    * hits are sorted by (-score, source_document_id, page_number, ordinal);
    * selection is a two-pass guarantee of page diversity: first one region per
      distinct page (by source_document_id + page_id), then remaining capacity
      filled by score order.
    """

    name = "deterministic_lexical"

    def attached_context(self, *, source_ids, project_ids, revision_ids=None, limit=24):
        """One representative, full-page context item per explicitly attached page.

        Mirrors the previous ``select_attached_context``: a single immutable
        page context item per (document, revision, page), in stable document /
        page / ordinal order, each scored 0 and marked as direct attachment.
        """
        queryset = SearchPassage.objects.filter(
            source_document_id__in=source_ids, project__in=project_ids,
        )
        if revision_ids:
            queryset = queryset.filter(processed_revision_id__in=revision_ids)
        passages = queryset.select_related(
            "source_document", "processed_revision", "processing_job", "page", "page_region",
        ).order_by("source_document_id", "page__page_number", "ordinal", "id")
        selected = []
        seen_pages = set()
        for passage in passages:
            page_key = (passage.source_document_id, passage.processed_revision_id, passage.page_id)
            if page_key in seen_pages:
                continue
            seen_pages.add(page_key)
            selected.append(EvidenceHit(
                score=0, passage=passage,
                method="direct-attachment", reason="attached-document/context",
            ))
            if len(selected) >= limit:
                break
        return selected

    def search(self, *, query, source_ids, project_ids, revision_ids=None, limit=24):
        """Rank matching passages deterministically with page-diversity coverage."""
        normalized_question = normalize_text(query)
        tokens = [token for token in re.findall(r"[\w-]{3,}", normalized_question)]
        queryset = SearchPassage.objects.filter(
            source_document_id__in=source_ids, project__in=project_ids,
        )
        if revision_ids:
            queryset = queryset.filter(processed_revision_id__in=revision_ids)
        passages = list(queryset.select_related(
            "source_document", "processed_revision", "processing_job", "page", "page_region",
        ))
        scored = []
        for passage in passages:
            text = passage.normalized_text
            score = sum(text.count(token) for token in tokens)
            if normalized_question in text:
                score += 10
            if score:
                scored.append(EvidenceHit(score=score, passage=passage, method="deterministic_lexical", reason="__unselected__"))
        scored.sort(key=lambda hit: (
            -hit.score,
            hit.passage.source_document_id,
            hit.passage.page.page_number if hit.passage.page else 0,
            hit.passage.ordinal,
        ))
        selected = []
        seen_pages = set()
        # First pass guarantees page coverage among matching evidence.
        for hit in scored:
            page_key = (hit.passage.source_document_id, hit.passage.page_id)
            if page_key not in seen_pages:
                selected.append(EvidenceHit(score=hit.score, passage=hit.passage, method="deterministic_lexical", reason="query-match/page-coverage"))
                seen_pages.add(page_key)
            if len(selected) >= limit:
                break
        # Second pass fills remaining capacity by score order.
        for hit in scored:
            if len(selected) >= limit:
                break
            if not any(existing.passage.id == hit.passage.id for existing in selected):
                selected.append(EvidenceHit(score=hit.score, passage=hit.passage, method="deterministic_lexical", reason="query-match"))
        return selected
