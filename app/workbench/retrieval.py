"""Chat evidence retrieval boundary.

Milestone 2A extracted passage selection/ranking out of chat orchestration
into a small ``EvidenceRetriever`` interface without intentionally changing
retrieval behavior; ``DeterministicLexicalRetriever`` reproduces the original
algorithm exactly and is the reference/fallback implementation.

Milestone 2B adds a second, conservative fuzzy/lexical retriever
(``FuzzyLexicalRetriever``) behind the same boundary for historical/OCR
material. It targets OCR character mistakes, punctuation noise, line-break
hyphenation, Unicode normalization, German inflection/minor spelling variation,
partial names and slightly corrupted archival identifiers while still giving
exact phrases a strong boost.

Both retrievers share the same tie-breaking (explicit score, then stable
document/page/ordinal order) and the same two-pass page-diversity selection, and
every hit keeps ``score``/``method``/``reason`` provenance for diagnostics. No
embeddings, DB full-text or external services are introduced.
"""
import re
import time
import unicodedata
from dataclasses import dataclass

from django.db.models import Q

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


# ---------------------------------------------------------------------------
# Fuzzy lexical retrieval (Milestone 2B)
# ---------------------------------------------------------------------------

#: Passages fully scanned per scope before we fall back to DB-assisted bounded
#: candidate prefiltering. The deterministic retriever already full-scans a
#: scope, so for corpora this small a full fuzzy scan is the honest baseline.
FUZZY_FULL_SCAN_MAX = 30000

#: Fuzzy matching is only applied to tokens at least this long, so short/common
#: German terms never explode into fuzzy noise.
FUZZY_MIN_TOKEN_LEN = 5

#: Hard cap on edit distance for a fuzzy token match (conservative for OCR).
FUZZY_MAX_EDIT_DISTANCE = 2

#: Minimum similarity (1 - dist/max_len) for a fuzzy token match.
FUZZY_MIN_SIMILARITY = 0.8

#: Per fuzzy-matched query token we add a small bounded contribution so exact
#: matches stay dominant, and we cap the total fuzzy-only contribution.
FUZZY_TOKEN_SCORE = 1
FUZZY_TOKEN_SCORE_CAP = 3

#: Exact whole-question phrase boost (strong).
EXACT_PHRASE_BOOST = 25
#: Punctuation- and hyphen-tolerated whole-phrase boost (still exact words).
ALPHA_PHRASE_BOOST = 12
#: Boost when the query resolves against the archival identifier.
ARCHIVAL_ID_BOOST = 15
#: Small boost for heading passages that carry a query token.
HEADING_BOOST = 5


def lexical_tokens(text):
    """Unicode letter/digit tokens (len>=3) from already-normalized text.
    Uses word characters minus underscore so German names and umlauts are kept.
    """
    return [t for t in re.findall(r"[^\W_]+", text or "") if len(t) >= 3]


def alpha(text):
    """Lowercase letters/digits only. Removes all punctuation AND spaces, so a
    hyphen break ('ver- breiten') and simple spelling noise both collapse to
    the same contiguous form and hyphenated/punctuation-damaged phrases match.
    """
    return re.sub(r"[^\w]", "", (text or "").lower())


def _ascii_fold(value):
    """Strip diacritics (NFKD then drop combining marks); used only for fuzzy
    comparison, never stored. Lets ä/ö/ü and historical variants
    (e.g. Schäffer/Schaffer) still match at a small distance."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _edit_distance(a, b, max_dist=None):
    """Levenshtein distance with an optional early-exit bound."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    bound = min(max_dist, max(len(a), len(b)) + 1) if max_dist else max(len(a), len(b)) + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = cur[0]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
            if cur[-1] < row_min:
                row_min = cur[-1]
        if row_min > bound:
            return bound + 1
        prev = cur
    return prev[-1]


def fuzzy_token_match(query_token, passage_token):
    """True when two tokens are close enough (same length class, small edit
    distance, high similarity) to count as an OCR-tolerant match. The token must
    be long enough that a small edit is meaningful, which keeps short/common
    terms from producing fuzzy noise. An exact equality is never reported as a
    fuzzy match (exact matches are handled by the lexical counts instead)."""
    q = _ascii_fold(query_token)
    p = _ascii_fold(passage_token)
    if len(q) < FUZZY_MIN_TOKEN_LEN or len(p) < FUZZY_MIN_TOKEN_LEN:
        return False
    if q == p:
        # Diacritic-only difference (e.g. Schäffer/Schaffer) or an exact raw
        # token; exact raw matches are guarded by the caller, so both count.
        return True
    longer = max(len(q), len(p))
    dist = _edit_distance(q, p, max_dist=FUZZY_MAX_EDIT_DISTANCE)
    if dist > FUZZY_MAX_EDIT_DISTANCE:
        return False
    if 1 - (dist / longer) < FUZZY_MIN_SIMILARITY:
        return False
    return True


def _best_fuzzy_token(query_token, passage_tokens):
    for candidate in passage_tokens:
        if fuzzy_token_match(query_token, candidate):
            return candidate
    return None


def _passage_token_index(text):
    """Word tokens (with spaces preserved) used for fuzzy matching, with
    diacritics stripped so OCR-ish accents line up. Takes the normalized,
    space-separated text (not the space-free alpha form)."""
    return [t for t in re.findall(r"[^\W_]+", _ascii_fold(text or "")) if len(t) >= FUZZY_MIN_TOKEN_LEN]


class FuzzyLexicalRetriever(EvidenceRetriever):
    """Conservative lexical/fuzzy retriever for historical/OCR material.

    Keeps the deterministic retriever's guarantees (explicit non-probability
    score, stable tie-breaking, two-pass page diversity, strict source/project/
    revision filtering) and adds tolerant matching for common OCR failures.
    Exact phrases still get the strongest boost so fuzzy never drowns clear
    lexical evidence.
    """

    name = "fuzzy_lexical"

    def __init__(self):
        self.last_stats = {"passages_scanned": 0, "hits_returned": 0, "retrieval_ms": 0.0}

    def attached_context(self, *, source_ids, project_ids, revision_ids=None, limit=24):
        """Identical direct-attachment semantics to the deterministic retriever:
        one full-page context item per (document, revision, page). Fuzzy matching
        is irrelevant here because these are explicit, authoritative attachments.
        """
        return DeterministicLexicalRetriever().attached_context(
            source_ids=source_ids, project_ids=project_ids,
            revision_ids=revision_ids, limit=limit,
        )

    def _scope_passages(self, *, source_ids, project_ids, revision_ids):
        queryset = SearchPassage.objects.filter(
            source_document_id__in=source_ids, project__in=project_ids,
        )
        if revision_ids:
            queryset = queryset.filter(processed_revision_id__in=revision_ids)
        scope_count = queryset.count()
        selected_rows = list(queryset.select_related(
            "source_document", "processed_revision", "processing_job", "page", "page_region",
        ))
        return selected_rows, scope_count

    def search(self, *, query, source_ids, project_ids, revision_ids=None, limit=24):
        started = time.monotonic()
        normalized_question = normalize_text(query)
        all_tokens = lexical_tokens(normalized_question)
        # Fuzzy only on longer, informative tokens.
        fuzz_tokens = [t for t in all_tokens if len(t) >= FUZZY_MIN_TOKEN_LEN]
        query_alpha = alpha(normalized_question)

        rows, scope_count = self._scope_passages(
            source_ids=source_ids, project_ids=project_ids, revision_ids=revision_ids,
        )
        passages = rows
        # Large-scope bound: restrict to passages sharing any exact token overlap
        # so we never pay O(N * fuzzy) on a huge corpus.
        if scope_count > FUZZY_FULL_SCAN_MAX and all_tokens:
            passages = [
                p for p in passages
                if p.normalized_text and any(tok in p.normalized_text for tok in all_tokens)
            ]

        scored = []
        scanned = 0
        for passage in passages:
            scanned += 1
            text = passage.normalized_text or ""
            if not text:
                continue
            score, reason = self._score_passage(
                passage, normalized_question, query_alpha, all_tokens, fuzz_tokens, text,
            )
            if score > 0:
                scored.append(EvidenceHit(score=score, passage=passage, method=self.name, reason=reason))

        scored.sort(key=lambda hit: (
            -hit.score,
            hit.passage.source_document_id,
            hit.passage.page.page_number if hit.passage.page else 0,
            hit.passage.ordinal,
        ))
        selected, selected_ids = [], set()
        seen_pages = set()
        for hit in scored:
            page_key = (hit.passage.source_document_id, hit.passage.page_id)
            if page_key not in seen_pages:
                selected.append(EvidenceHit(score=hit.score, passage=hit.passage, method=self.name, reason=hit.reason + "/page-coverage"))
                seen_pages.add(page_key)
                selected_ids.add(hit.passage.id)
            if len(selected) >= limit:
                break
        for hit in scored:
            if len(selected) >= limit:
                break
            if hit.passage.id not in selected_ids:
                selected.append(EvidenceHit(score=hit.score, passage=hit.passage, method=self.name, reason=hit.reason))
                selected_ids.add(hit.passage.id)
        self.last_stats = {
            "passages_scanned": scanned,
            "hits_returned": len(selected),
            "retrieval_ms": round((time.monotonic() - started) * 1000, 3),
        }
        return selected

    def _score_passage(self, passage, normalized_question, query_alpha, all_tokens, fuzz_tokens, text):
        score = 0
        reasons = []
        p_alpha = alpha(text)
        archival = (passage.archival_identifier or "").upper()
        heading_ctx = (passage.heading_context or "").lower()

        # 1. Exact whole-question phrase: strongest signal.
        if normalized_question and normalized_question in text:
            score += EXACT_PHRASE_BOOST
            reasons.append("exact-phrase")

        # 2. Punctuation- and hyphen-tolerated phrase (exact words, noisy surface).
        elif query_alpha and len(query_alpha) >= 8 and query_alpha in p_alpha:
            score += ALPHA_PHRASE_BOOST
            reasons.append("punctuation-tolerated-phrase")

        # 3. Lexical token counts (identical semantics to deterministic) plus a
        # small per-distinct-token presence bonus so an exact match always
        # outranks a merely fuzzy match of the same term.
        matched_exact = False
        for token in all_tokens:
            count = text.count(token)
            if count:
                score += count + 1
                matched_exact = True
        if matched_exact:
            reasons.append("token-match")

        # 4. Archival identifier boost (slightly corrupted / partial identifier).
        if archival and query_alpha:
            if query_alpha in alpha(archival) or any(token.upper() in archival for token in all_tokens):
                score += ARCHIVAL_ID_BOOST
                reasons.append("archival-id")

        # 5. Heading boost.
        if passage.passage_type == "heading" and (matched_exact or heading_ctx):
            if any(token in text for token in all_tokens):
                score += HEADING_BOOST
                reasons.append("heading-boost")

        # 6. Conservative fuzzy token match for OCR damage not found above.
        if fuzz_tokens:
            passage_tokens = _passage_token_index(text)
            fuzzy_gain = 0
            fuzzy_hits = 0
            for qtok in fuzz_tokens:
                # skip tokens already present exactly (counted above)
                if text.count(qtok):
                    continue
                if _best_fuzzy_token(qtok, passage_tokens):
                    fuzzy_hits += 1
                    fuzzy_gain += FUZZY_TOKEN_SCORE
            if fuzzy_hits:
                score += min(fuzzy_gain, FUZZY_TOKEN_SCORE_CAP)
                reasons.append("fuzzy-token")

        return score, "fuzzy-lexical/" + self._pick_reason(reasons)

    @staticmethod
    def _pick_reason(reasons):
        for pref in ("exact-phrase", "punctuation-tolerated-phrase", "archival-id", "token-match", "heading-boost", "fuzzy-token"):
            if pref in reasons:
                return pref
        return "token-match"


_RETRIEVER_REGISTRY = {
    "deterministic_lexical": DeterministicLexicalRetriever,
    "fuzzy_lexical": FuzzyLexicalRetriever,
}


def get_retriever(name=None):
    """Return the configured retriever, falling back to the deterministic
    reference for invalid/unknown/unsupported values so a bad setting can never
    break retrieval. ``name`` may be an explicit string (used by the eval harness
    to exercise a specific implementation) or ``None`` to read the configured
    ``DSW_CHAT_RETRIEVER``.
    """
    if not name:
        from django.conf import settings as dj_settings
        name = getattr(dj_settings, "DSW_CHAT_RETRIEVER", "deterministic_lexical")
    cls = _RETRIEVER_REGISTRY.get(name) if isinstance(name, str) else None
    if cls is None:
        return DeterministicLexicalRetriever()
    return cls()
