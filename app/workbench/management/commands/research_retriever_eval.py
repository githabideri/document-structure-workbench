"""Compare retrieval implementations over the Phase-0 baseline question set.

Runs every selected retriever (deterministic_lexical, fuzzy_lexical) over the
same questions in the same scopes and records, per case: the resolved scope,
top-N hits (source file, page, passage type, score, reason, snippet), and the
retriever's own latency / passages scanned / hits returned. Retrieval latency is
measured independently of any provider, so fuzzy-vs-deterministic cost can be
compared directly.

Relevance judgment uses an inline ``EXPECTED_SOURCES`` mapping (source filenames
known to be relevant to a question) so top-3 / top-8 relevant-hit rates and
obvious-false-positive flags can be computed across the two retrievers. Run
``--inventory`` first to see the real corpus filenames and tune the mapping.

Usage:
  manage.py research_retriever_eval [--retrievers a,b] [--top N] [--out file.json]
                                   [--cases id,...] [--inventory]
"""
import argparse
import json
import os
import time

from django.core.management.base import BaseCommand

from workbench.models import Collection, SourceDocument
from workbench.retrieval import get_retriever

# app/workbench/management/commands -> app/workbench
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASELINE_FIXTURE = os.path.join(HERE, "tests", "fixtures", "chat", "research_baseline_cases.json")


#: Hard OCR / historical cases added beyond the 10 Phase-0 cases to stress the
#: fuzzy retriever (misspelt names, hyphenation, historical spelling, and a
#: question whose relevant passage does not contain the exact surface form).
HARD_CASES = [
    {
        "id": "hard-01",
        "category": 11,
        "category_label": "OCR-damaged personal name",
        "question": "Schachermair an der Spitze der Gemeindeverwaltung?",
        "scope": {"mode": "project", "project_id": "Korrespondenz"},
        "expected_sources": [],
    },
    {
        "id": "hard-02",
        "category": 12,
        "category_label": "hyphenated compound",
        "question": "Reiseverordnung in Freistadt",
        "scope": {"mode": "project", "project_id": "Korrespondenz"},
        "expected_sources": [],
    },
    {
        "id": "hard-03",
        "category": 13,
        "category_label": "historical place spelling / OCR",
        "question": "Fremdenverkehrsambt Freistatt fersteher",
        "scope": {"mode": "project", "project_id": "Korrespondenz"},
        "expected_sources": [],
    },
    {
        "id": "hard-04",
        "category": 14,
        "category_label": "no fuzzy garbage on nothing",
        "question": "Unbürgermeisterzusammenkunft 1820 in Freistadt",
        "scope": {"mode": "project", "project_id": "Korrespondenz"},
        "expected_sources": [],
    },
]


def _load_baseline():
    with open(BASELINE_FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)["cases"]


class Command(BaseCommand):
    help = "Evaluate retrieval implementations over the Phase-0 baseline + hard cases."

    def add_arguments(self, parser):
        parser.add_argument("--retrievers", default="deterministic_lexical,fuzzy_lexical")
        parser.add_argument("--top", type=int, default=8)
        parser.add_argument("--out", default="")
        parser.add_argument("--cases", default="")
        parser.add_argument("--expected", default="", help="JSON literal {case_id: [source filenames]}")
        parser.add_argument("--inventory", action="store_true", help="List corpus sources per project and exit.")

    def handle(self, *args, **options):
        self.top = options["top"]
        if options["inventory"]:
            return self._inventory()
        cases = _load_baseline() + HARD_CASES
        if options["cases"]:
            wanted = {c.strip() for c in options["cases"].split(",") if c.strip()}
            cases = [c for c in cases if c["id"] in wanted]
        expected = json.loads(options["expected"]) if options["expected"] else {}
        retrievers = [get_retriever(name.strip()) for name in options["retrievers"].split(",") if name.strip()]
        report = {
            "title": "Research/Chat V2 retriever comparison",
            "top": self.top,
            "retrievers": [r.name for r in retrievers],
            "corpus": self._corpus_inventory(),
            "cases": [],
        }
        for case in cases:
            row = self._evaluate_case(case, retrievers, expected.get(case["id"]))
            report["cases"].append(row)
        payload = json.dumps(report, indent=2, ensure_ascii=False)
        if options["out"]:
            with open(options["out"], "w", encoding="utf-8") as handle:
                handle.write(payload)
            self.stdout.write("Report written to %s" % options["out"])
        self.stdout.write(self._render_summary(report))

    def _corpus_inventory(self):
        rows = []
        for project in Collection.objects.all().order_by("id"):
            sources = list(SourceDocument.objects.filter(collection=project).order_by("filename"))
            rows.append({
                "project_id": project.id,
                "project": project.name,
                "sources": [s.filename for s in sources],
            })
        return rows

    def _inventory(self):
        for row in self._corpus_inventory():
            self.stdout.write(
                "[%s] %s\n    %s" % (
                    row["project_id"], row["project"],
                    "\n    ".join(row["sources"]) or "(no sources)",
                )
            )

    def _resolve_scope(self, case):
        scope = case.get("scope") or {}
        attached = case.get("attached_documents") or []
        expected = case.get("expected_sources") or []
        source_ids = list(SourceDocument.objects.filter(filename__in=attached).values_list("pk", flat=True))
        pid_slug = scope.get("project_id")
        projects = []
        if pid_slug:
            for project in Collection.objects.all():
                matches = project.name == pid_slug
                if not matches and hasattr(project, "slug"):
                    matches = project.slug == pid_slug
                if matches:
                    projects.append(project.pk)
                    break
        if not projects:
            # Fall back to projects owning the attached / expected sources.
            names = list(set(attached) | set(expected))
            if names:
                projects = list(SourceDocument.objects.filter(filename__in=names).values_list("collection_id", flat=True).distinct())
        if not projects:
            projects = list(Collection.objects.all().values_list("pk", flat=True))
        return {"mode": scope.get("mode", "project"), "source_ids": source_ids, "project_ids": projects}

    def _evaluate_case(self, case, retrievers, expected_sources):
        expected_sources = set(expected_sources or case.get("expected_sources") or [])
        scope = self._resolve_scope(case)
        row = {
            "id": case["id"],
            "category_label": case.get("category_label", ""),
            "question": case["question"],
            "scope": scope,
            "expected_sources": sorted(expected_sources),
            "retrievers": {},
        }
        for retriever in retrievers:
            started = time.monotonic()
            hits = retriever.search(
                query=case["question"],
                source_ids=scope["source_ids"],
                project_ids=scope["project_ids"],
                limit=self.top,
            )
            wall_ms = round((time.monotonic() - started) * 1000, 3)
            stats = dict(getattr(retriever, "last_stats", {}) or {})
            stats["wall_ms"] = wall_ms
            hit_rows = []
            for hit in hits:
                p = hit.passage
                hit_rows.append({
                    "source": p.source_document.filename,
                    "page": p.page.page_number if p.page else None,
                    "type": p.passage_type,
                    "score": hit.score,
                    "reason": hit.reason,
                    "snippet": (p.text or "")[:120].replace("\n", " "),
                    "relevant": p.source_document.filename in expected_sources if expected_sources else None,
                })
            row["retrievers"][retriever.name] = {
                "hits": hit_rows,
                "stats": stats,
                "top3_relevant": self._relevant_count(hit_rows[:3], expected_sources),
                "top8_relevant": self._relevant_count(hit_rows[:8], expected_sources),
                "false_positive": self._false_positive(hit_rows, expected_sources),
            }
        return row

    @staticmethod
    def _relevant_count(hits, expected_sources):
        if not expected_sources:
            return None
        return sum(1 for h in hits if h["relevant"])

    @staticmethod
    def _false_positive(hits, expected_sources):
        # A clear false positive is a hit from a project outside the case's scope,
        # or (when expected sources are known) a top hit that is not relevant at all.
        if expected_sources:
            return not any(h["relevant"] for h in hits)
        return None

    def _render_summary(self, report):
        lines = []
        lines.append("\n=== Retriever comparison (%s) top-%s ===" % (", ".join(report["retrievers"]), report["top"]))
        header = "%-11s %-8s %-9s %-9s %8s %9s %8s %8s" % (
            "case", "det-t3", "det-t8", "fl-t3", "fl-t8", "det-ms", "fl-ms", "det-scn")
        lines.append(header)
        for case in report["cases"]:
            det = case["retrievers"].get("deterministic_lexical", {})
            fl = case["retrievers"].get("fuzzy_lexical", {})
            d3 = det.get("top3_relevant", "n/a"); d8 = det.get("top8_relevant", "n/a")
            f3 = fl.get("top3_relevant", "n/a"); f8 = fl.get("top8_relevant", "n/a")
            dms = det.get("stats", {}).get("wall_ms", 0)
            fms = fl.get("stats", {}).get("wall_ms", 0)
            dscn = det.get("stats", {}).get("passages_scanned", 0)
            lines.append("%-11s %-8s %-9s %-9s %8s %9s %8s %8s" % (
                case["id"], d3, d8, f3, f8, dms, fms, dscn))
        lines.append("(t3/t8 = relevant hits within top-3/top-8 when expected sources are known; '-' = no expected annotation)")
        return "\n".join(lines)
