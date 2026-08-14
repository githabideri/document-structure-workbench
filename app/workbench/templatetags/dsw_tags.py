"""Reusable template filters for DSW."""
import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

from ..validation import verdict_summary

register = template.Library()


@register.filter(name="review_labels")
def review_labels(verdict):
    """Human-readable review reasons of a persisted validation verdict.

    Takes the ``metadata['validation']`` dict of an ``OcrRequest`` (resolved
    through the template attribute chain) and returns the translated reason
    labels joined for a badge tooltip; empty string when no review is needed.
    """
    summary = verdict_summary(verdict)
    return ", ".join(summary["labels"]) if summary["needs_review"] else ""


@register.filter(name="needs_review")
def needs_review(verdict):
    """Whether a persisted validation verdict flags the candidate for review."""
    return verdict_summary(verdict)["needs_review"]


@register.filter(name="highlight")
def highlight(text, query):
    """HTML-escape ``text`` and wrap case-insensitive matches of ``query`` in <mark>.

    The text is escaped first, so user/extracted content can never inject markup;
    only the literal ``<mark>`` wrapper is added. Matching is case-insensitive to
    mirror the normalized substring search, though it does not replicate unicode
    normalization — a matched passage is always shown, just not always highlighted
    when its normalization differs from the query.
    """
    text = "" if text is None else str(text)
    query = "" if query is None else str(query).strip()
    escaped = escape(text)
    if not query:
        return mark_safe(escaped)
    pattern = re.compile(re.escape(query), re.IGNORECASE | re.DOTALL)
    return mark_safe(pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped))
