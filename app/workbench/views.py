"""
Archive Structure Workbench - Views.
"""
import csv
import io
import json
import secrets
from html.parser import HTMLParser
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.contrib.auth.models import Group
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q, Avg, F
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.contrib.auth import logout as auth_logout

from .models import (
    PROJECT_CURATOR_GROUP,
    AuditEvent, Collection, Decision, Document, ExtractionRun,
    Page, PageRegion, ProcessingArtifact, RegionCorrection, Review, ReviewTask, SourceDocument, TableCandidate,
    TableExtraction, OcrRequest,
)
from .models import LANGUAGES
from .export import DEFAULT_FORMAT, FORMATS, DocumentExportService


# --- Permission helpers ---

def is_reviewer(user):
    return user.groups.filter(name__in=["Reviewer", "Curator", "Administrator"]).exists()


def is_curator(user):
    return user.groups.filter(name__in=["Curator", "Administrator"]).exists()


def is_admin(user):
    return user.is_superuser or user.groups.filter(name="Administrator").exists()


def is_project_curator(user):
    """Members of the Project Curator group (plus superusers and admins) can
    do all project lifecycle work: create, archive/restore, and manage
    projects. Membership is seeded for all existing users by migration and is
    changeable by an administrator in the Django admin or via the
    manage_project_curator management command."""
    return (
        user.is_superuser
        or user.groups.filter(name__in=[PROJECT_CURATOR_GROUP, "Administrator"]).exists()
    )


# --- Project access decorator ---

def require_project_access(view_func, project_kwarg="collection_id"):
    """Decorator that enforces project membership before rendering.

    Skips check for global administrators.
    """
    from functools import wraps
    from .policy import ProjectAccessPolicy

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        # Global admins bypass
        if is_admin(request.user):
            return view_func(request, *args, **kwargs)

        project_id = kwargs.get(project_kwarg)
        if project_id is None:
            return view_func(request, *args, **kwargs)

        from .models import Collection
        project = Collection.objects.filter(pk=project_id).first()
        if not project:
            return view_func(request, *args, **kwargs)

        policy = ProjectAccessPolicy(user=request.user)
        if not policy.can_view(project):
            messages.error(request, _("You do not have access to this project."))
            return redirect("collection_list")

        return view_func(request, *args, **kwargs)
    return wrapper


# --- Audit helper ---

def log_audit(request, event_type, object_type="", object_id="", before=None, after=None):
    AuditEvent.objects.create(
        actor=request.user if request.user.is_authenticated else None,
        event_type=event_type,
        object_type=object_type,
        object_id=str(object_id),
        before=before,
        after=after,
    )


def logout_view(request):
    """Custom logout that accepts GET (for simple link-based logout)."""
    auth_logout(request)
    return redirect("login")


# --- Candidate assignment ---

def assign_candidate_order(task, first, second):
    """Atomically assign candidates X/Y to a review task."""
    if secrets.randbelow(2) == 0:
        task.candidate_x = first
        task.candidate_y = second
    else:
        task.candidate_x = second
        task.candidate_y = first
    task.save(update_fields=["candidate_x", "candidate_y"])


# --- Dashboard ---

@login_required
def dashboard(request):
    from .models import ProcessingJob, SourceDocument
    from .policy import ProjectAccessPolicy

    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()
    editable_projects = [project for project in visible_projects if policy.can_edit(project)]

    recent_documents = list(
        SourceDocument.objects.filter(
            collection__in=visible_projects,
            is_archived=False,
        ).select_related("collection", "active_document")[:6]
    )
    for source in recent_documents:
        source.latest_job = source.processing_jobs.first()

    active_states = ["queued", "submitting", "processing", "importing"]
    active_jobs = ProcessingJob.objects.filter(
        source_document__collection__in=visible_projects,
        state__in=active_states,
    ).select_related("source_document", "source_document__collection")[:6]
    attention_jobs = ProcessingJob.objects.filter(
        source_document__collection__in=visible_projects,
        state__in=["submission_uncertain", "interrupted", "partial", "failed"],
    ).select_related("source_document", "source_document__collection")[:6]

    tasks = ReviewTask.objects.filter(assigned_to=request.user)
    completed = tasks.filter(state="completed").count()
    in_progress = tasks.filter(state="in_progress").count()
    unassigned = tasks.filter(state="unassigned").count()
    needs_expert = tasks.filter(state="needs_expert").count()
    skipped = tasks.filter(state="skipped").count()
    total = tasks.count()

    return render(request, "workbench/dashboard.html", {
        "username": request.user.get_username(),
        "total_tasks": total,
        "completed": completed,
        "in_progress": in_progress,
        "unassigned": unassigned,
        "needs_expert": needs_expert,
        "skipped": skipped,
        "is_reviewer": is_reviewer(request.user),
        "is_curator": is_curator(request.user),
        "is_admin": is_admin(request.user),
        "can_add_document": bool(editable_projects),
        "recent_documents": recent_documents,
        "active_jobs": active_jobs,
        "attention_jobs": attention_jobs,
    })


# --- Collections ---

@login_required
def collection_list(request):
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    collections = list(Collection.objects.all() if is_admin(request.user) else policy.visible_projects())
    for collection in collections:
        collection.user_can_edit = policy.can_edit(collection)
    return render(request, "workbench/collection_list.html", {
        "collections": collections,
        "can_add_document": any(project.user_can_edit for project in collections),
        "can_create_project": is_project_curator(request.user),
        "is_admin": is_admin(request.user),
    })


@login_required
def project_create(request):
    # Project lifecycle work is reserved for Project Curators (and admins).
    if not is_project_curator(request.user):
        raise PermissionDenied
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        source_type = request.POST.get("source_type", "corpus")
        if not name:
            messages.error(request, _("Enter a project name."))
        elif Collection.objects.filter(name=name).exists():
            messages.error(request, _("A project with this name already exists."))
        elif source_type not in {choice[0] for choice in Collection._meta.get_field("source_type").choices}:
            messages.error(request, _("Choose a valid project type."))
        else:
            project = Collection.objects.create(
                name=name,
                description=request.POST.get("description", "").strip(),
                source_type=source_type,
                created_by=request.user,
            )
            from .models import ProjectMembership
            ProjectMembership.objects.create(project=project, user=request.user, role="owner")
            log_audit(request, "project_created", "Collection", project.id)
            return redirect("collection_detail", collection_id=project.id)
    return render(request, "workbench/project_form.html", {"project": None})


@login_required
@require_POST
def project_archive(request, collection_id):
    if not is_project_curator(request.user):
        raise PermissionDenied
    from .policy import ProjectAccessPolicy
    project = get_object_or_404(Collection, pk=collection_id)
    from .services import LifecycleError, ProjectLifecycleService
    try:
        ProjectLifecycleService.archive_project(project=project, policy=ProjectAccessPolicy(user=request.user), archived=not project.is_archived)
    except (PermissionError, LifecycleError) as exc:
        messages.error(request, _(str(exc)))
        return redirect("collection_list")
    log_audit(request, "project_archived" if project.is_archived else "project_restored", "Collection", project.id,
              after={"is_archived": project.is_archived})
    messages.success(request, _("Project status updated."))
    return redirect("collection_list")


@login_required
@require_POST
def source_archive(request, source_id):
    from .policy import ProjectAccessPolicy
    from .services import LifecycleError, ProjectLifecycleService
    source = get_object_or_404(SourceDocument.objects.select_related("collection"), pk=source_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_edit(source.collection):
        raise PermissionDenied
    try:
        ProjectLifecycleService.archive_source(source=source, policy=policy, archived=not source.is_archived)
    except (PermissionError, LifecycleError) as exc:
        messages.error(request, _(str(exc)))
        return redirect("collection_detail", collection_id=source.collection_id)
    log_audit(request, "source_archived" if source.is_archived else "source_restored", "SourceDocument", source.id,
              after={"is_archived": source.is_archived})
    messages.success(request, _("Upload status updated."))
    return redirect("collection_detail", collection_id=source.collection_id)


@login_required
def collection_detail(request, collection_id):
    from .policy import ProjectAccessPolicy
    collection = get_object_or_404(Collection, pk=collection_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_view(collection):
        messages.error(request, _("You do not have access to this project."))
        return redirect("collection_list")

    show_archived = request.GET.get("archived") == "1" and policy.can_edit(collection)
    source_documents = list(
        collection.source_documents.filter(is_archived=show_archived).select_related("active_document")
    )
    for source in source_documents:
        source.latest_job = source.processing_jobs.first()
    tables = TableCandidate.objects.filter(document__collection=collection)
    reviews = Review.objects.filter(review_task__table_candidate__document__collection=collection)

    return render(request, "workbench/collection_detail.html", {
        "collection": collection,
        "source_documents": source_documents,
        "can_edit_project": policy.can_edit(collection),
        "total_tables": tables.count(),
        "total_reviews": reviews.count(),
        "show_archived": show_archived,
    })


# --- Documents ---

@login_required
def document_list(request):
    from .models import SourceDocument
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_collections = policy.visible_projects()
    documents = SourceDocument.objects.filter(
        is_archived=False,
        collection__in=visible_collections,
    ).select_related("collection", "active_document")
    collection_id = request.GET.get("collection")
    if collection_id:
        documents = documents.filter(collection_id=collection_id)

    documents = list(documents)
    for document in documents:
        document.latest_job = document.processing_jobs.first()
    editable_projects = [project for project in visible_collections if policy.can_edit(project)]

    return render(request, "workbench/document_list.html", {
        "documents": documents,
        "can_add_document": bool(editable_projects),
    })


@login_required
def search_view(request):
    """Search only projects visible to the current user and cite revisions."""
    from .policy import ProjectAccessPolicy
    from .search import search_project
    policy = ProjectAccessPolicy(user=request.user)
    projects = list(policy.visible_projects())
    query = request.GET.get("q", "").strip()
    source_ids = request.GET.getlist("source")
    results = search_project(projects, query, source_ids=source_ids) if query else []
    for result in results:
        result.citation_url = (
            f"{reverse('document_detail', args=[result.source_document_id])}"
            f"?revision={result.processed_revision_id}&page={result.page.page_number if result.page else 1}"
            f"&region={result.page_region_id or ''}"
        )
        result.snippet = _search_snippet(result.text, query)
    # Stable document-then-reading-order sort so {% regroup %} can fold hits by
    # document without interleaving. There is no relevance score yet.
    results.sort(key=lambda r: (
        (r.source_document.filename or "").casefold(),
        r.page.page_number if r.page else 0,
        r.ordinal,
    ))
    return render(request, "workbench/search.html", {
        "query": query,
        "results": results,
        "projects": projects,
    })


def _search_snippet(text, query, window=240):
    """Return a match-centered context window of ``text`` (full text if short)."""
    text = text or ""
    if len(text) <= window:
        return text
    idx = text.lower().find((query or "").lower())
    if idx < 0:
        return text[:window].rstrip() + "\u2026"
    start = max(0, idx - window // 3)
    end = min(len(text), start + window)
    prefix = "\u2026" if start > 0 else ""
    suffix = "\u2026" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


@login_required
def search_reader(request, passage_id):
    """Return an inline reader fragment (scan + highlighted text) for one passage.

    HTMX target of the search result cards. Kept as its own partial so the same
    scan-left/text-right rendering can later back the HTML export.
    """
    from .policy import ProjectAccessPolicy
    from .models import SearchPassage
    passage = get_object_or_404(
        SearchPassage.objects.select_related(
            "page", "page_region", "source_document", "processed_revision", "project",
        ),
        pk=passage_id,
    )
    if not ProjectAccessPolicy(user=request.user).can_view(passage.project):
        raise PermissionDenied
    region = passage.page_region
    citation_url = (
        f"{reverse('document_detail', args=[passage.source_document_id])}"
        f"?revision={passage.processed_revision_id}"
        f"&page={passage.page.page_number if passage.page else 1}"
        f"&region={passage.page_region_id or ''}"
    )
    return render(request, "workbench/_search_reader.html", {
        "passage": passage,
        "query": request.GET.get("q", "").strip(),
        "citation_url": citation_url,
        "region_left": region.left if region else 0,
        "region_top": region.top if region else 0,
        "region_width": (region.right - region.left) if region else 0,
        "region_height": (region.bottom - region.top) if region else 0,
    })


@login_required
def chat_view(request):
    from .policy import ProjectAccessPolicy
    from .chat import create_chat_run
    from .models import ChatThread
    policy = ProjectAccessPolicy(user=request.user)
    projects = list(policy.visible_projects())
    sources = list(SourceDocument.objects.filter(collection__in=projects, is_archived=False).select_related("collection", "active_document")[:100])
    visible_project_ids = {p.id for p in projects}
    show_archived = request.GET.get("archived") == "1"
    thread_queryset = ChatThread.objects.filter(is_archived=show_archived)
    thread_queryset = [thread for thread in thread_queryset.select_related("project")
                       if thread.project_id in visible_project_ids or
                       visible_project_ids.intersection((thread.scope_config or {}).get("project_ids", []))]
    if not is_admin(request.user):
        thread_queryset = [thread for thread in thread_queryset if thread.created_by_id == request.user.id]
    threads = sorted(thread_queryset, key=lambda item: (item.updated_at, item.id), reverse=True)[:50]
    error = None
    selected_ids = request.POST.getlist("source") if request.method == "POST" else request.GET.getlist("source")
    selected_mode = request.POST.get("scope_mode", "all") if request.method == "POST" else request.GET.get("scope_mode", "all")
    selected_project_id = request.POST.get("project_id") if request.method == "POST" else request.GET.get("project_id")
    if request.method == "POST":
        question = request.POST.get("question", "").strip()
        if not question:
            error = _("Choose at least one document or project scope and enter a question.")
        else:
            from .policy import ProjectAccessPolicy
            try:
                scope = policy.resolve_chat_scope(
                    mode=selected_mode, project_id=int(selected_project_id) if selected_project_id else None,
                    source_ids=selected_ids,
                )
                project = Collection.objects.filter(pk=selected_project_id).first() if selected_project_id else None
                thread = ChatThread.objects.create(
                    project=project, created_by=request.user, title=question[:120],
                    model=getattr(settings, "DSW_CHAT_MODEL", ""),
                    scope_mode=selected_mode, scope_config=scope,
                    selected_revisions=scope["revision_ids"],
                )
                thread.selected_sources.set(SourceDocument.objects.filter(pk__in=scope["attachment_ids"]))
                create_chat_run(thread, question, scope=scope)
                return redirect("chat_thread", thread_id=thread.id)
            except (ValueError, PermissionError):
                error = _("The selected scope or documents are not accessible.")
    return render(request, "workbench/chat.html", {
        "sources": sources, "projects": projects, "selected_ids": set(selected_ids),
        "selected_mode": selected_mode, "selected_project_id": selected_project_id, "error": error,
        "thread": None, "chat_messages": [], "latest_run": None,
        "threads": threads, "is_admin": is_admin(request.user), "show_archived": show_archived,
    })


@login_required
def chat_thread_view(request, thread_id):
    from .policy import ProjectAccessPolicy
    from .models import ChatRun, ChatThread, SourceDocument
    from .chat import create_chat_run, resolve_followup_scope, _default_followup_scope, render_message_with_citations
    thread_queryset = ChatThread.objects.prefetch_related("selected_sources", "messages")
    if not is_admin(request.user):
        thread_queryset = thread_queryset.filter(created_by=request.user)
    thread = get_object_or_404(thread_queryset, pk=thread_id)
    policy = ProjectAccessPolicy(user=request.user)
    if thread.project_id and not policy.can_view(thread.project):
        return redirect("chat")
    if not thread.project_id and not set((thread.scope_config or {}).get("project_ids", [])) & set(policy.visible_projects().values_list("id", flat=True)):
        return redirect("chat")
    error = None
    if request.method == "POST":
        question = request.POST.get("question", "").strip()
        if question:
            # A collapsed, untouched source editor submits scope_inherit=1 and
            # inherits the previous run's frozen scope verbatim. Changing any
            # scope control clears the flag so the edited configuration is
            # re-resolved (and re-authorized) before the run is frozen.
            inherit = request.POST.get("scope_inherit") == "1"
            posted_scope = None if inherit else {
                "mode": request.POST.get("scope_mode"),
                "project_id": request.POST.get("project_id") or None,
                "attachment_ids": [int(value) for value in request.POST.getlist("source") if value],
            }
            try:
                scope = resolve_followup_scope(policy, thread, scope=posted_scope)
            except (ValueError, PermissionError):
                error = _("The selected scope or documents are not accessible.")
            else:
                create_chat_run(thread, question, scope=scope)
                return redirect("chat_thread", thread_id=thread.id)
    sources = list(thread.selected_sources.select_related("collection"))
    visible_projects = policy.visible_projects()
    visible_project_ids = set(visible_projects.values_list("id", flat=True))
    projects = list(visible_projects)
    source_options = list(SourceDocument.objects.filter(collection__in=visible_projects, is_archived=False).select_related("collection", "active_document")[:200])
    default_scope = _default_followup_scope(thread)
    thread_queryset = [thread for thread in ChatThread.objects.filter(is_archived=False).select_related("project")
                       if thread.project_id in visible_project_ids or
                       visible_project_ids.intersection((thread.scope_config or {}).get("project_ids", []))]
    if not is_admin(request.user):
        thread_queryset = [thread for thread in thread_queryset if thread.created_by_id == request.user.id]
    threads = sorted(thread_queryset, key=lambda item: (item.updated_at, item.id), reverse=True)[:50]
    messages = list(thread.messages.all())
    assistant_runs = {
        run.assistant_message_id: run
        for run in ChatRun.objects.filter(
            assistant_message__in=messages,
        ).prefetch_related("evidence_items")
    }
    for message in messages:
        message.rendered_text = render_message_with_citations(
            message, assistant_runs.get(message.id)
        )
    latest_run = thread.runs.prefetch_related("evidence_items").order_by("-created_at").first()
    selected_run = thread.runs.filter(pk=request.GET.get("run", "0")).prefetch_related("evidence_items").first() if request.GET.get("run") else latest_run
    evidence_items = list(selected_run.evidence_items.select_related("source_document", "page") if selected_run else [])
    run_error = selected_run.error_message if selected_run and selected_run.state == "failed" else None
    return render(request, "workbench/chat.html", {
        "sources": sources, "source_options": source_options, "projects": projects,
        "default_scope": default_scope, "selected_ids": {str(source.id) for source in sources},
        "thread": thread, "chat_messages": messages, "latest_run": latest_run, "selected_run": selected_run,
        "evidence_items": evidence_items,
        "selected_evidence_marker": request.GET.get("evidence", ""),
        "error": error or run_error,
        "threads": threads, "is_admin": is_admin(request.user),
    })


@login_required
@require_POST
def chat_thread_rename(request, thread_id):
    from .models import ChatThread
    from .policy import ProjectAccessPolicy
    queryset = ChatThread.objects.all() if is_admin(request.user) else ChatThread.objects.filter(created_by=request.user)
    thread = get_object_or_404(queryset, pk=thread_id)
    from .services import ChatManagementService
    try:
        ChatManagementService.rename(thread, request.POST.get("title", ""), ProjectAccessPolicy(user=request.user))
        messages.success(request, _("Conversation renamed."))
    except (PermissionError, ValueError) as exc:
        messages.error(request, _(str(exc)))
    return redirect("chat_thread", thread_id=thread.id)


@login_required
@require_POST
def chat_thread_archive(request, thread_id):
    from .models import ChatThread
    from .policy import ProjectAccessPolicy
    queryset = ChatThread.objects.all() if is_admin(request.user) else ChatThread.objects.filter(created_by=request.user)
    thread = get_object_or_404(queryset, pk=thread_id)
    from .services import ChatManagementService
    try:
        ChatManagementService.archive(thread, ProjectAccessPolicy(user=request.user), archived=not thread.is_archived)
    except PermissionError:
        raise PermissionDenied
    messages.success(request, _("Conversation status updated."))
    return redirect("chat")


@login_required
def chat_run_status(request, run_id):
    from .models import ChatRun
    run = get_object_or_404(ChatRun.objects.select_related("thread", "assistant_message"), pk=run_id, thread__created_by=request.user)
    if run.state in {"completed", "failed", "cancelled"}:
        response = HttpResponse("")
        response["HX-Redirect"] = reverse("chat_thread", args=[run.thread_id])
        return response
    return render(request, "workbench/_chat_run_status.html", {"run": run})


@login_required
def chat_evidence_detail(request, run_id, marker):
    """Session-authenticated evidence representation for the contextual reader."""
    from .models import ChatRun
    from .policy import ProjectAccessPolicy
    run = get_object_or_404(ChatRun.objects.select_related("thread", "thread__project"), pk=run_id)
    if not is_admin(request.user) and run.thread.created_by_id != request.user.id:
        raise PermissionDenied
    policy = ProjectAccessPolicy(user=request.user)
    if run.thread.project_id and not policy.can_view(run.thread.project):
        raise PermissionDenied
    if not run.thread.project_id and not set((run.scope_snapshot or {}).get("project_ids", [])) & set(policy.visible_projects().values_list("id", flat=True)):
        raise PermissionDenied
    item = get_object_or_404(run.evidence_items.select_related("source_document", "processed_revision", "page", "page_region"), marker=marker)
    if not policy.can_view(item.source_document.collection):
        raise PermissionDenied
    page = item.page
    region = item.page_region
    document_url = reverse("document_detail", args=[item.source_document_id]) + f"?revision={item.processed_revision_id}&page={page.page_number if page else 1}"
    if region:
        document_url += f"&region={region.id}"
    return JsonResponse({
        "run_id": run.id, "marker": item.marker, "filename": item.source_document.filename,
        "project": item.source_document.collection.name, "revision_id": item.processed_revision_id,
        "page_id": page.id if page else None, "page_number": page.page_number if page else None,
        "region_id": region.id if region else None, "region_type": region.effective_region_type if region else None,
        "image_url": reverse("page_image", args=[page.id]) if page and page.image_path else None,
        "passage": item.text, "page_text": item.page_text or item.text,
        "score": item.score, "selection_reason": item.selection_reason,
        "document_url": document_url, "created_at": run.created_at.isoformat(),
    })


@login_required
def chat_run_diagnostics(request, run_id):
    """Show sensitive run diagnostics only to Administrator maintainers."""
    if not is_admin(request.user):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    from .models import ChatRun
    run = get_object_or_404(
        ChatRun.objects.select_related("thread", "thread__project", "assistant_message", "user_message")
        .prefetch_related("evidence_items", "events"), pk=run_id,
    )
    from .services import SupportBundleService
    bundle = SupportBundleService.build(run)
    inspector = SupportBundleService.diagnostics(run)
    diagnostics = {
        "run": bundle["run"], "project": bundle["project"], "scope": bundle["scope"],
        "question": bundle["question"], "evidence": bundle["evidence"],
        "provider": bundle["provider"], "final_answer": bundle["final_answer"],
        "reasoning_content": bundle.get("reasoning_content"), "events": bundle["events"],
    }
    tool_events = [event for event in bundle["events"] if event["name"] in {"tool_call", "tool_fallback", "tool_call_rejected", "tool_call_limit", "final_answer_request", "final_answer_rejected", "citation_rejected"}]
    used_model_tools = bool([event for event in bundle["events"] if event["name"] == "tool_call"])
    retrieval_event = next((event for event in bundle["events"] if event["name"] == "evidence_selected"), {})
    retriever_event = next((event for event in bundle["events"] if event["name"] == "retriever"), {})
    run_meta = bundle["run"].get("model_metadata") or {}
    active_retriever = (
        retriever_event.get("metadata", {}).get("implementation")
        or run_meta.get("retriever")
        or getattr(settings, "DSW_CHAT_RETRIEVER", "deterministic_lexical")
    )
    provider_summary = {
        "configured_mode": getattr(settings, "DSW_CHAT_TOOL_MODE", "fallback"),
        "actual_mode": "model-requested search" if used_model_tools else "server-side deterministic retrieval",
        "retriever": active_retriever,
        "retriever_configured": getattr(settings, "DSW_CHAT_RETRIEVER", "deterministic_lexical"),
        "retriever_source": (
            "run-event" if retriever_event else ("run-metadata" if run_meta.get("retriever") else "configured-default")
        ),
        "tool_events": tool_events,
        "model": (bundle.get("provider") or {}).get("model", settings.DSW_CHAT_MODEL),
        "evidence_count": retrieval_event.get("metadata", {}).get("count", len(bundle["evidence"])),
    }
    return render(request, "workbench/chat_run_diagnostics.html", {
        "run": run, "diagnostics": diagnostics, "inspector": inspector, "timeline": SupportBundleService.human_timeline(bundle),
        "provider_summary": provider_summary, "diagnostics_json": json.dumps(diagnostics, indent=2, ensure_ascii=False),
    })


@login_required
def chat_run_diagnostics_data(request, run_id):
    if not is_admin(request.user):
        raise PermissionDenied
    from .models import ChatRun
    from .services import SupportBundleService
    run = get_object_or_404(ChatRun.objects.select_related("thread", "user_message"), pk=run_id)
    return JsonResponse(SupportBundleService.diagnostics(run))


@login_required
def help_page(request):
    """Plain-language orientation for the primary document workflow."""
    return render(request, "workbench/help.html")


class _PageTextToPlain(HTMLParser):
    """Convert raw (possibly HTML-laced) page text into readable plain text.

    Visual-OCR models sometimes return the page transcription as HTML (e.g. an
    inline ``<table>``). The "Complete page text" readout should not dump that
    raw markup. This converter drops tags, breaks table rows/cells onto new
    lines, and unescapes entities, yielding a legible transcript.
    """

    _BLOCK_TAGS = {
        "tr", "p", "div", "li", "h1", "h2", "h3", "h4", "h5",
        "table", "thead", "tbody", "tfoot", "caption", "ul", "ol",
        "section", "blockquote", "pre", "br", "hr",
    }
    _CELL_TAGS = {"td", "th"}

    def __init__(self):
        super().__init__()
        self._parts = []
        self._pending_space = False

    def handle_starttag(self, tag, attrs):
        self._handle(tag)

    def handle_startendtag(self, tag, attrs):
        self._handle(tag)

    def handle_endtag(self, tag):
        if tag.lower() in self._BLOCK_TAGS:
            self._emit_break()

    def _handle(self, tag):
        tag = tag.lower()
        if tag in self._CELL_TAGS:
            self._pending_space = True
        elif tag in self._BLOCK_TAGS:
            self._emit_break()

    def handle_data(self, data):
        text = data.replace("\u00a0", " ").strip()
        if not text:
            return
        if self._pending_space and self._parts and not self._parts[-1].endswith(" "):
            self._parts.append(" ")
        self._pending_space = False
        self._parts.append(text)

    def _emit_break(self):
        if self._parts and self._parts[-1] == " ":
            self._parts.pop()
        if self._parts and self._parts[-1].endswith("\n"):
            return
        self._parts.append("\n")
        self._pending_space = False

    def result(self):
        text = "".join(self._parts)
        lines = [line.rstrip() for line in text.split("\n")]
        out = []
        blank = False
        for line in lines:
            if not line.strip():
                if blank:
                    continue
                blank = True
            else:
                blank = False
            out.append(line)
        return "\n".join(out).strip()


def page_text_to_plain(text):
    """Render raw page text as readable plain text (HTML if present)."""
    if not text:
        return ""
    import html

    if "<" in text and ">" in text:
        parser = _PageTextToPlain()
        try:
            parser.feed(text)
            parser.close()
            return parser.result()
        except Exception:
            return html.unescape(text)
    return html.unescape(text)


@login_required
def document_detail(request, document_id):
    from .policy import ProjectAccessPolicy
    revision_id = request.GET.get("revision")
    source_document = None
    if revision_id:
        # Citation URLs use the stable source ID plus an immutable revision ID:
        # /documents/<source-id>/?revision=<revision-id>&page=&region=
        source_document = get_object_or_404(
            SourceDocument.objects.select_related("collection"), pk=document_id,
        )
        document = get_object_or_404(
            Document.objects.select_related("collection", "processing_job__source_document"),
            pk=revision_id,
            processing_job__source_document=source_document,
        )
    else:
        # Keep legacy revision URLs functional while callers migrate to the
        # explicit source/revision form.
        document = get_object_or_404(
            Document.objects.select_related("collection", "processing_job__source_document"),
            pk=document_id,
        )
        source_document = getattr(getattr(document, "processing_job", None), "source_document", None)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_view(document.collection):
        messages.error(request, _("You do not have access to this document."))
        return redirect("document_list")
    can_edit_document = policy.can_edit(document.collection)

    pages = list(document.pages.all())
    page_number = request.GET.get("page")
    try:
        requested_page = int(page_number) if page_number else 0
    except (TypeError, ValueError):
        requested_page = 0
    page = next((item for item in pages if item.page_number == requested_page), None)
    if page is None and pages:
        page = pages[0]
    page_index = pages.index(page) if page in pages else -1
    previous_page = pages[page_index - 1] if page_index > 0 else None
    next_page = pages[page_index + 1] if page_index >= 0 and page_index + 1 < len(pages) else None

    regions = list(
        PageRegion.objects.filter(page__document=document)
        .select_related("page", "job")
        .prefetch_related("corrections", "ocr_requests")
    )
    for region in regions:
        region.display_region_type = region.effective_region_type
        region.display_region_type_label = dict(PageRegion.REGION_TYPES).get(
            region.display_region_type, region.display_region_type,
        )
        region.display_is_suppressed = region.is_suppressed
        region.suppression_correction = region.corrections.filter(
            operation="suppress", status="active",
        ).order_by("-created_at", "-id").first()
        region.overlay_width = region.right - region.left
        region.overlay_height = region.bottom - region.top
        region.overlay_left_percent = region.left * 100
        region.overlay_top_percent = region.top * 100
        region.overlay_width_percent = region.overlay_width * 100
        region.overlay_height_percent = region.overlay_height * 100
    page_regions = [region for region in regions if page and region.page_id == page.id]
    selected_region = None
    region_id = request.GET.get("region")
    if region_id:
        try:
            selected_region = next(
                (region for region in page_regions if region.id == int(region_id)),
                None,
            )
        except (TypeError, ValueError):
            selected_region = None

    page_text = ""
    page_ocr_requests = list(OcrRequest.objects.filter(page=page).exclude(provider="htr").order_by("-created_at", "-id")[:10]) if page else []
    processing_job = getattr(document, "processing_job", None)
    if processing_job:
        artifact = ProcessingArtifact.objects.filter(
            job=processing_job, artifact_type="page_text", page_number=page.page_number if page else None,
        ).first()
        if artifact:
            page_text = artifact.data.get("text", "") if isinstance(artifact.data, dict) else ""
        page_text = page_text_to_plain(page_text)

    tables = document.tables.filter(page=page).select_related("page").prefetch_related("extractions") if page else []
    for table in tables:
        for extraction in table.extractions.all():
            if extraction.raw_html:
                import bleach
                extraction.safe_html = bleach.clean(
                    extraction.raw_html,
                    tags=["table", "thead", "tbody", "tr", "th", "td", "caption", "p", "br"],
                    attributes={"th": ["colspan", "rowspan"], "td": ["colspan", "rowspan"]},
                    strip=True,
                )

    visual_ocr_requests = (
        list(selected_region.ocr_requests.exclude(provider="htr").order_by("-created_at", "-id")[:10])
        if selected_region else []
    )
    return render(request, "workbench/document_detail.html", {
        "document": document,
        "tables": tables,
        "pages": pages,
        "selected_page": page,
        "previous_page": previous_page,
        "next_page": next_page,
        "page_regions": page_regions,
        "region_types": PageRegion.REGION_TYPES,
        "selected_region": selected_region,
        "page_text": page_text,
        "page_ocr_requests": page_ocr_requests,
        "visual_ocr_requests": visual_ocr_requests,
        "region_ocr_fragment_url": (
            reverse("ocr_history_fragment", args=[source_document.id]) + f"?region={selected_region.id}"
            if source_document and selected_region else ""
        ),
        "page_ocr_fragment_url": (
            reverse("ocr_history_fragment", args=[source_document.id]) + f"?page={page.id}"
            if source_document and page else ""
        ),
        "region_ocr_any_pending": any(c.state in {"queued", "processing"} for c in visual_ocr_requests),
        "page_ocr_any_pending": any(c.state in {"queued", "processing"} for c in page_ocr_requests),
        "processing_job": processing_job,
        "source_document": source_document,
        "workspace_document_id": source_document.id if source_document else document.id,
        "workspace_revision_id": document.id,
        "can_edit_document": can_edit_document,
        "thread_id": request.GET.get("thread", ""),
        "htr_enabled": getattr(settings, "DSW_HTR_ENABLED", False) or getattr(settings, "DSW_HTR_FIXTURE_MODE", False),
        "htr_default_pipeline": getattr(settings, "DSW_HTR_DEFAULT_PIPELINE", "htrflow-trocr-kurrent"),
        "htr_pipelines": [
            ("htrflow-trocr-kurrent", "TrOCR · Kurrent (19th-c. German)"),
            ("htrflow-trocr-prototype", "TrOCR · prototype (generic)"),
        ],
    })


@login_required
@require_POST
def correct_region_text(request, region_id):
    """Apply one explicit, reversible text correction to a region."""
    from .services import CorrectionError, CorrectionService
    region = get_object_or_404(
        PageRegion.objects.select_related("page__document__collection", "source_document"),
        pk=region_id,
    )
    document = region.page.document
    expected = request.POST.get("expected_current_text", "")
    replacement = request.POST.get("replacement_text", "")
    if region.effective_text != expected:
        messages.error(request, _("This region changed since it was inspected. Reload it before editing."))
    elif not replacement.strip():
        messages.error(request, _("Replacement text cannot be empty."))
    else:
        try:
            CorrectionService.apply(
                region=region, user=request.user, operation="text",
                before={"text": region.effective_text}, after={"text": replacement},
                reason=request.POST.get("reason", "").strip(),
            )
        except CorrectionError as error:
            messages.error(request, _(str(error)))
        else:
            messages.success(request, _("Correction saved. The original machine extraction remains unchanged."))
    source = getattr(getattr(document, "processing_job", None), "source_document", None)
    target = source.id if source else document.id
    query = f"?revision={document.id}&page={region.page_number}&region={region.id}" if source else f"?page={region.page_number}&region={region.id}"
    return redirect(f"{reverse('document_detail', args=[target])}{query}")


@login_required
@require_POST
def correct_region(request, region_id):
    """Apply a typed correction operation to one immutable-revision region."""
    from .services import CorrectionError, CorrectionService
    region = get_object_or_404(PageRegion.objects.select_related("page__document__collection", "source_document"), pk=region_id)
    document = region.page.document
    operation = request.POST.get("operation", "")
    expected = request.POST.get("expected_current_value", "")
    if operation == "type":
        value = request.POST.get("region_type", "")
        if value not in dict(PageRegion.REGION_TYPES):
            messages.error(request, _("Choose a valid region type."))
            return _redirect_to_region(request, region)
        current = region.effective_region_type
        after, before = {"region_type": value}, {"region_type": current}
    elif operation == "suppress":
        after, before = {"suppressed": True}, {"suppressed": region.is_suppressed}
        current = "true" if region.is_suppressed else "false"
    elif operation == "note":
        note = request.POST.get("note", "").strip()
        if not note:
            messages.error(request, _("A note cannot be empty."))
            return _redirect_to_region(request, region)
        after, before = {"note": note}, {}
        current = ""
    else:
        messages.error(request, _("Unsupported correction operation."))
        return _redirect_to_region(request, region)
    if operation in {"type", "suppress"} and expected != current:
        messages.error(request, _("This region changed since it was inspected. Reload it before editing."))
        return _redirect_to_region(request, region)
    try:
        CorrectionService.apply(
            region=region, user=request.user, operation=operation,
            before=before, after=after, reason=request.POST.get("reason", "").strip(),
        )
    except CorrectionError as error:
        messages.error(request, _(str(error)))
        return _redirect_to_region(request, region)
    messages.success(request, _("Correction saved. The original machine extraction remains unchanged."))
    return _redirect_to_region(request, region)


def _redirect_to_region(request, region):
    source = getattr(getattr(region.page.document, "processing_job", None), "source_document", None)
    target = source.id if source else region.page.document_id
    query = f"?revision={region.page.document_id}&page={region.page_number}&region={region.id}" if source else f"?page={region.page_number}&region={region.id}"
    return redirect(f"{reverse('document_detail', args=[target])}{query}")


@login_required
def ocr_history_fragment(request, document_id):
    """Render just the OCR-candidates history so polling never reloads the page
    image. Called by the region/page detail JS while a candidate is pending."""
    from .policy import ProjectAccessPolicy
    from .models import OcrRequest, PageRegion, SourceDocument
    source = get_object_or_404(SourceDocument.objects.select_related("collection"), pk=document_id)
    if not ProjectAccessPolicy(user=request.user).can_view(source.collection):
        raise PermissionDenied
    region_id = request.GET.get("region")
    page_id = request.GET.get("page")
    candidates = []
    ocr_kind = "region"
    ocr_title = _("Visual OCR candidates")
    if region_id:
        region = PageRegion.objects.filter(
            pk=int(region_id),
            page__document__processing_job__source_document_id=source.pk,
        ).first()
        if region:
            candidates = list(region.ocr_requests.exclude(provider="htr").order_by("-created_at", "-id")[:10])
    elif page_id:
        page = get_object_or_404(Page, pk=int(page_id), document__processing_job__source_document_id=source.pk)
        ocr_kind = "page"
        ocr_title = _("Page OCR candidates")
        candidates = list(OcrRequest.objects.filter(page=page).exclude(provider="htr").order_by("-created_at", "-id")[:10])
    any_pending = any(c.state in {"queued", "processing"} for c in candidates)
    return render(request, "workbench/_ocr_history_fragment.html", {
        "ocr_candidates": candidates,
        "ocr_kind": ocr_kind,
        "ocr_title": ocr_title,
        "fragment_url": request.get_full_path(),
        "any_pending": any_pending,
        "can_edit_document": ProjectAccessPolicy(user=request.user).can_edit(source.collection),
    })


@login_required
@require_POST
def create_ocr_request(request, region_id):
    """Queue a visual OCR candidate for one immutable detected region."""
    from .policy import ProjectAccessPolicy
    from .models import PageRegion
    region = get_object_or_404(PageRegion.objects.select_related("page__document__collection", "page__document__processing_job__source_document"), pk=region_id)
    if not ProjectAccessPolicy(user=request.user).can_edit(region.page.document.collection):
        raise PermissionDenied
    from .services import OcrService
    provider = (request.POST.get("provider") or getattr(settings, "DSW_OCR_PROVIDER", "qwen")).strip()
    try:
        OcrService.create(
            page=region.page, region=region, provider=provider,
            model=(getattr(settings, "DSW_CHAT_MODEL", "") if provider == "qwen" else getattr(settings, "DSW_OCR_MODEL", "")),
            prompt=request.POST.get("prompt", ""), user=request.user,
        )
    except (PermissionError, ValueError) as exc:
        messages.error(request, _(str(exc)))
        return _redirect_to_region(request, region)
    messages.success(request, _("Visual OCR candidate queued. This will not change the current text automatically."))
    return _redirect_to_region(request, region)


@login_required
@require_POST
def create_page_ocr_request(request, page_id):
    """Queue visual OCR for an entire immutable page image."""
    from .policy import ProjectAccessPolicy
    page = get_object_or_404(Page.objects.select_related("document__collection", "document__processing_job__source_document"), pk=page_id)
    if not ProjectAccessPolicy(user=request.user).can_edit(page.document.collection):
        raise PermissionDenied
    from .services import OcrService
    provider = (request.POST.get("provider") or getattr(settings, "DSW_OCR_PROVIDER", "qwen")).strip()
    try:
        OcrService.create(
            page=page, provider=provider,
            model=(getattr(settings, "DSW_CHAT_MODEL", "") if provider == "qwen" else getattr(settings, "DSW_OCR_MODEL", "")),
            prompt=request.POST.get("prompt", ""), user=request.user,
        )
    except (PermissionError, ValueError) as exc:
        messages.error(request, _(str(exc)))
        return redirect(f"{reverse('document_detail', args=[page.document.processing_job.source_document.id])}?revision={page.document_id}&page={page.page_number}")
    messages.success(request, _("Full-page visual OCR candidate queued."))
    source = page.document.processing_job.source_document
    return redirect(f"{reverse('document_detail', args=[source.id])}?revision={page.document_id}&page={page.page_number}")


@login_required
@require_POST
def accept_ocr_request(request, request_id):
    from .policy import ProjectAccessPolicy
    item = get_object_or_404(OcrRequest.objects.select_related("region", "document__collection"), pk=request_id)
    if not ProjectAccessPolicy(user=request.user).can_edit(item.document.collection):
        raise PermissionDenied
    from .services import OcrService
    try:
        OcrService.accept(item=item, user=request.user)
        messages.success(request, _("Visual OCR text accepted as a reversible correction."))
    except (PermissionError, ValueError) as exc:
        messages.error(request, _(str(exc)))
    return _redirect_to_region(request, item.region)


@login_required
@require_POST
def revert_region_correction(request, correction_id):
    from .policy import ProjectAccessPolicy
    correction = get_object_or_404(RegionCorrection.objects.select_related("document__collection"), pk=correction_id)
    if not ProjectAccessPolicy(user=request.user).can_edit(correction.document.collection):
        return JsonResponse({"error": "permission_denied"}, status=403)
    if correction.status == "active":
        correction.status = "reverted"
        correction.reverted_at = timezone.now()
        correction.save(update_fields=["status", "reverted_at"])
    return redirect(request.POST.get("next") or reverse("document_detail", args=[correction.document_id]))


# --- Reviews ---

@login_required
def review_list(request):
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()

    # Only show tasks from projects the user can access
    tasks = ReviewTask.objects.filter(
        assigned_to=request.user,
        table_candidate__document__collection__in=visible_projects
    )
    if is_curator(request.user):
        tasks = ReviewTask.objects.filter(
            table_candidate__document__collection__in=visible_projects
        )

    return render(request, "workbench/review_list.html", {"tasks": tasks})


@login_required
def review_next(request):
    """Get the next unassigned or assigned review task."""
    task = (
        ReviewTask.objects
        .filter(assigned_to=request.user, state__in=["unassigned", "assigned"])
        .order_by("priority", "created_at")
        .first()
    )
    if not task:
        return redirect("review_list")

    return redirect("review_detail", task_id=task.id)


@login_required
def review_skip(request, task_id):
    """Skip a task for now (keeps it available, doesn't count as completed)."""
    from .policy import ProjectAccessPolicy
    task = get_object_or_404(ReviewTask, pk=task_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_task(task):
        return redirect("review_list")
    if task.assigned_to != request.user and not is_curator(request.user):
        return redirect("review_list")

    task.state = "skipped"
    task.save(update_fields=["state"])
    log_audit(request, "task_skipped", "ReviewTask", task.id)

    messages.info(request, _("Task skipped. It will remain available for later."))
    return redirect("review_list")


@login_required
def review_needs_expert(request, task_id):
    """Mark a task as needing expert review."""
    from .policy import ProjectAccessPolicy
    task = get_object_or_404(ReviewTask, pk=task_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_task(task):
        return redirect("review_list")
    if task.assigned_to != request.user and not is_curator(request.user):
        return redirect("review_list")

    task.state = "needs_expert"
    task.save(update_fields=["state"])
    log_audit(request, "task_needs_expert", "ReviewTask", task.id)

    messages.info(request, _("Task marked as needing expert review."))
    return redirect("review_list")


@login_required
def review_detail(request, task_id):
    from .policy import ProjectAccessPolicy
    task = get_object_or_404(ReviewTask, pk=task_id)

    # Check project access
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_task(task):
        messages.error(request, _("You do not have access to this task."))
        return redirect("review_list")

    # Check if user is assigned or is a curator
    if task.assigned_to != request.user and not is_curator(request.user):
        return redirect("review_list")

    # Check if already reviewed
    existing_review = None
    if Review.objects.filter(review_task=task).exists():
        existing_review = Review.objects.get(review_task=task)

    # Get extractions for this table
    extractions = task.table_candidate.extractions.select_related("extraction_run")
    a_extraction = extractions.filter(extraction_run__profile="standard-docling").first()
    b_extraction = extractions.filter(extraction_run__profile="granite-table-crop").first()
    gt_extraction = extractions.filter(extraction_run__profile="ground-truth").first()

    # Ensure candidate ordering is stable
    if not task.candidate_x_id or not task.candidate_y_id:
        if a_extraction and b_extraction:
            with transaction.atomic():
                assign_candidate_order(task, a_extraction, b_extraction)

    # Determine candidates from task assignment
    if task.candidate_x_id and task.candidate_y_id:
        candidate_x = task.candidate_x
        candidate_y = task.candidate_y
    elif a_extraction and b_extraction:
        candidate_x = a_extraction
        candidate_y = b_extraction
    else:
        candidate_x = a_extraction
        candidate_y = b_extraction

    # Mark task as in progress
    if task.state not in ["in_progress", "completed", "needs_expert"]:
        task.state = "in_progress"
        task.assigned_to = request.user
        task.save(update_fields=["state", "assigned_to"])

    # Sanitize HTML for display
    import bleach
    allowed_tags = bleach.sanitizer.ALLOWED_TAGS | {"table", "tbody", "tr", "td", "th", "caption", "thead", "tfoot"}
    allowed_attrs = {"td": ["rowspan", "colspan"], "th": ["rowspan", "colspan"]}

    # Check for missing extractions
    x_missing = candidate_x is None or candidate_x.status == "missing" or not candidate_x.raw_html
    y_missing = candidate_y is None or candidate_y.status == "missing" or not candidate_y.raw_html

    return render(request, "workbench/review_detail.html", {
        "task": task,
        "table_candidate": task.table_candidate,
        "document": task.table_candidate.document,
        "candidate_x": candidate_x,
        "candidate_y": candidate_y,
        "candidate_x_html": bleach.clean(candidate_x.raw_html, tags=allowed_tags, attributes=allowed_attrs) if candidate_x and candidate_x.raw_html else "",
        "candidate_y_html": bleach.clean(candidate_y.raw_html, tags=allowed_tags, attributes=allowed_attrs) if candidate_y and candidate_y.raw_html else "",
        "x_missing": x_missing,
        "y_missing": y_missing,
        "existing_review": existing_review,
        "is_blinded": not existing_review,
        "has_gt": gt_extraction is not None and gt_extraction.raw_html,
        "gt_extraction": gt_extraction,
        "gt_html": bleach.clean(gt_extraction.raw_html, tags=allowed_tags, attributes=allowed_attrs) if gt_extraction and gt_extraction.raw_html else "",
        "is_curator": is_curator(request.user),
        "show_guidelines_link": True,
    })


@login_required
@require_POST
def review_submit(request, task_id):
    """Submit a review. Uses Post/Redirect/Get pattern."""
    from .policy import ProjectAccessPolicy
    task = get_object_or_404(ReviewTask, pk=task_id)

    # Check project access
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_task(task):
        messages.error(request, _("You do not have access to this task."))
        return redirect("review_list")

    # Check if user is assigned or is a curator
    if task.assigned_to != request.user and not is_curator(request.user):
        return redirect("review_list")

    # Check for existing review (duplicate submission)
    existing = Review.objects.filter(review_task=task).first()
    if existing is not None:
        messages.info(
            request,
            _("This review was already submitted. Your original response has been preserved."),
        )
        return redirect("review_reveal", task_id=task.pk)

    # Get extractions from task assignment
    candidate_x = task.candidate_x
    candidate_y = task.candidate_y

    if not candidate_x or not candidate_y:
        messages.error(request, _("Missing extraction data for this table."))
        return redirect("review_detail", task_id=task_id)

    # Create review atomically
    with transaction.atomic():
        review = Review.objects.create(
            review_task=task,
            reviewer=request.user,
            candidate_x=candidate_x,
            candidate_y=candidate_y,
            candidate_x_score=int(request.POST.get("candidate_x_score", 0)),
            candidate_y_score=int(request.POST.get("candidate_y_score", 0)),
            preferred_result=request.POST.get("preferred_result", "equivalent"),
            structure_errors=request.POST.getlist("structure_errors"),
            text_errors=request.POST.getlist("text_errors"),
            comment=request.POST.get("comment", ""),
            confidence=request.POST.get("confidence", "medium"),
        )

        task.state = "completed"
        task.completed_at = review.created_at
        task.save(update_fields=["state", "completed_at"])

    log_audit(request, "review_created", "Review", review.id)

    messages.success(request, _("Review submitted successfully."))
    return redirect("review_reveal", task_id=task.pk)


@login_required
def review_reveal(request, task_id):
    """Show the reveal page after review submission."""
    from .policy import ProjectAccessPolicy
    task = get_object_or_404(ReviewTask, pk=task_id)

    # Check project access
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_task(task):
        messages.error(request, _("You do not have access to this task."))
        return redirect("review_list")

    # Check if user is assigned or is a curator
    if task.assigned_to != request.user and not is_curator(request.user):
        return redirect("review_list")

    review = Review.objects.filter(review_task=task).first()
    if not review:
        return redirect("review_detail", task_id=task.pk)

    # Get ground truth
    gt_extraction = task.table_candidate.extractions.filter(
        extraction_run__profile="ground-truth"
    ).first()

    import bleach
    allowed_tags = bleach.sanitizer.ALLOWED_TAGS | {"table", "tbody", "tr", "td", "th", "caption", "thead", "tfoot"}
    allowed_attrs = {"td": ["rowspan", "colspan"], "th": ["rowspan", "colspan"]}

    return render(request, "workbench/review_reveal.html", {
        "task": task,
        "review": review,
        "gt_extraction": gt_extraction,
        "gt_html": bleach.clean(gt_extraction.raw_html, tags=allowed_tags, attributes=allowed_attrs) if gt_extraction and gt_extraction.raw_html else "",
        "has_gt": gt_extraction is not None and gt_extraction.raw_html,
        "is_curator": is_curator(request.user),
    })


@login_required
@require_POST
def review_post_reveal(request, task_id):
    """Add a post-reveal comment. Does not modify original scores."""
    from .policy import ProjectAccessPolicy
    task = get_object_or_404(ReviewTask, pk=task_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_task(task):
        return redirect("review_list")

    review = Review.objects.filter(review_task=task).first()

    if not review:
        return redirect("review_detail", task_id=task_id)

    review.post_reveal_comment = request.POST.get("post_reveal_comment", "")
    review.save(update_fields=["post_reveal_comment"])

    log_audit(request, "review_post_reveal", "Review", review.id, after={"post_reveal_comment": review.post_reveal_comment})

    messages.info(request, _("Post-reveal note saved."))
    return redirect("review_reveal", task_id=task.pk)


# --- Decisions ---

@login_required
@user_passes_test(is_curator)
def decision_list(request):
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()
    tables = TableCandidate.objects.filter(
        document__collection__in=visible_projects
    ).select_related("decision", "document")
    return render(request, "workbench/decision_list.html", {"tables": tables})


@login_required
@user_passes_test(is_curator)
def decision_detail(request, table_id):
    from .policy import ProjectAccessPolicy
    table = get_object_or_404(TableCandidate, pk=table_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_view(table.document.collection):
        messages.error(request, _("You do not have access to this table."))
        return redirect("decision_list")

    decision = None
    if hasattr(table, "decision"):
        decision = table.decision
    extractions = table.extractions.select_related("extraction_run")
    review = None
    if hasattr(table, "review_task"):
        review = Review.objects.filter(review_task=table.review_task).first()

    return render(request, "workbench/decision_detail.html", {
        "table": table,
        "decision": decision,
        "extractions": extractions,
        "review": review,
    })


@login_required
@user_passes_test(is_curator)
@require_POST
def decision_submit(request, table_id):
    from .policy import ProjectAccessPolicy
    table = get_object_or_404(TableCandidate, pk=table_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_view(table.document.collection):
        messages.error(request, _("You do not have access to this table."))
        return redirect("decision_list")

    extraction_id = request.POST.get("selected_extraction_id")

    extraction = None
    if extraction_id:
        extraction = get_object_or_404(TableExtraction, pk=extraction_id)

    decision, created = Decision.objects.update_or_create(
        table_candidate=table,
        defaults={
            "selected_extraction": extraction,
            "decision": request.POST.get("decision", "accepted"),
            "decided_by": request.user,
            "reason": request.POST.get("reason", ""),
        },
    )

    log_audit(request, "decision_created", "Decision", decision.id)

    return redirect("decision_list")


# --- Curator Dashboard ---

@login_required
@user_passes_test(is_curator)
def curator_dashboard(request):
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()
    collections = visible_projects

    total_documents = Document.objects.filter(
        is_archived=False, collection__in=visible_projects
    ).count()
    total_tables = TableCandidate.objects.filter(
        document__collection__in=visible_projects
    ).count()
    total_reviews = Review.objects.filter(
        review_task__table_candidate__document__collection__in=visible_projects
    ).count()
    pending_reviews = ReviewTask.objects.filter(
        state__in=["unassigned", "assigned", "in_progress"],
        table_candidate__document__collection__in=visible_projects
    ).count()

    # Preference distribution
    preferences = Review.objects.filter(
        review_task__table_candidate__document__collection__in=visible_projects
    ).values("preferred_result").annotate(count=Count("id"))

    # Error category totals
    error_counts = {}
    for review in Review.objects.filter(
        review_task__table_candidate__document__collection__in=visible_projects
    ).all():
        for err in review.structure_errors:
            error_counts[err] = error_counts.get(err, 0) + 1
        for err in review.text_errors:
            error_counts[err] = error_counts.get(err, 0) + 1

    return render(request, "workbench/curator_dashboard.html", {
        "collections": collections,
        "total_documents": total_documents,
        "total_tables": total_tables,
        "total_reviews": total_reviews,
        "pending_reviews": pending_reviews,
        "preferences": preferences,
        "error_counts": error_counts,
    })


@login_required
@user_passes_test(is_curator)
def technical_report(request):
    """Technical report with TEDS metrics."""
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()
    runs = ExtractionRun.objects.filter(
        document__collection__in=visible_projects
    ).select_related("document")
    extractions = TableExtraction.objects.filter(
        table_candidate__document__collection__in=visible_projects
    ).select_related("table_candidate", "extraction_run")

    stats = {}
    for profile in ["standard-docling", "granite-table-crop"]:
        profile_extractions = extractions.filter(
            extraction_run__profile=profile,
            teds_s__isnull=False,
        )
        if profile_extractions.exists():
            stats[profile] = {
                "count": profile_extractions.count(),
                "mean_teds_s": profile_extractions.aggregate(Avg("teds_s"))["teds_s__avg"],
                "mean_teds": profile_extractions.aggregate(Avg("teds"))["teds__avg"],
                "failures": profile_extractions.filter(status="failed").count(),
            }

    return render(request, "workbench/technical_report.html", {
        "stats": stats,
        "extractions": extractions,
    })


# --- Exports ---

@login_required
def export_revision(request, revision_id):
    """Download the effective (corrected) text of one processed revision."""
    from .policy import ProjectAccessPolicy

    fmt = request.GET.get("format", DEFAULT_FORMAT)
    if fmt not in FORMATS:
        return HttpResponseBadRequest(
            _(f"Unsupported export format '{fmt}'. Use one of: {', '.join(sorted(FORMATS))}.")
        )
    revision = get_object_or_404(
        Document.objects.select_related("collection", "processing_job__source_document"),
        pk=revision_id,
    )
    if not ProjectAccessPolicy(user=request.user).can_view(revision.collection):
        raise PermissionDenied
    data = DocumentExportService.revision_data(revision)
    renderer = DocumentExportService.to_markdown if fmt == "md" else DocumentExportService.to_text
    response = HttpResponse(renderer(data), content_type=FORMATS[fmt][0])
    response["Content-Disposition"] = (
        f'attachment; filename="{DocumentExportService.revision_filename(data, fmt)}"'
    )
    log_audit(request, "document_exported", "Document", revision.pk, after={"format": fmt})
    return response


@login_required
def export_project(request, project_id):
    """Download a ZIP (or single concatenated file) of every document in a project."""
    from .policy import ProjectAccessPolicy
    from .export import BUNDLES, DEFAULT_BUNDLE

    fmt = request.GET.get("format", DEFAULT_FORMAT)
    if fmt not in FORMATS:
        return HttpResponseBadRequest(
            _(f"Unsupported export format '{fmt}'. Use one of: {', '.join(sorted(FORMATS))}.")
        )
    bundle = request.GET.get("bundle", DEFAULT_BUNDLE)
    if bundle not in BUNDLES:
        return HttpResponseBadRequest(
            _(f"Unsupported bundle '{bundle}'. Use one of: {', '.join(sorted(BUNDLES))}.")
        )
    project = get_object_or_404(Collection, pk=project_id)
    if not ProjectAccessPolicy(user=request.user).can_view(project):
        raise PermissionDenied
    if bundle == "single":
        payload = DocumentExportService.render_singlefile(project, fmt)
        response = HttpResponse(payload, content_type=FORMATS[fmt][0])
    else:
        payload = DocumentExportService.render_zip(project, fmt)
        response = HttpResponse(payload, content_type="application/zip")
    response["Content-Disposition"] = (
        f'attachment; filename="{DocumentExportService.project_filename(project, fmt, bundle)}"'
    )
    log_audit(request, "project_exported", "Collection", project.pk, after={"format": fmt, "bundle": bundle})
    return response


@login_required
@user_passes_test(is_curator)
def export_reviews(request):
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()
    reviews = Review.objects.filter(
        review_task__table_candidate__document__collection__in=visible_projects
    ).select_related("review_task__table_candidate", "reviewer")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="reviews.csv"'

    writer = csv.writer(response)
    writer.writerow([
        "table_id", "document", "reviewer", "preferred_result", "candidate_x_score",
        "candidate_y_score", "confidence", "comment", "structure_errors", "text_errors",
        "created_at",
    ])

    for r in reviews:
        writer.writerow([
            r.review_task.table_candidate.stable_table_id,
            r.review_task.table_candidate.document.external_id,
            r.reviewer.username,
            r.preferred_result,
            r.candidate_x_score,
            r.candidate_y_score,
            r.confidence,
            r.comment,
            json.dumps(r.structure_errors),
            json.dumps(r.text_errors),
            r.created_at.isoformat(),
        ])

    log_audit(request, "export_created", "Export", "reviews.csv")
    return response


@login_required
@user_passes_test(is_curator)
def export_decisions(request):
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()
    decisions = Decision.objects.filter(
        table_candidate__document__collection__in=visible_projects
    ).select_related("table_candidate", "decided_by")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="decisions.csv"'

    writer = csv.writer(response)
    writer.writerow(["table_id", "document", "decision", "decided_by", "reason", "created_at"])

    for d in decisions:
        writer.writerow([
            d.table_candidate.stable_table_id,
            d.table_candidate.document.external_id,
            d.decision,
            d.decided_by.username,
            d.reason,
            d.created_at.isoformat(),
        ])

    log_audit(request, "export_created", "Export", "decisions.csv")
    return response


@login_required
@user_passes_test(is_curator)
def export_summary(request):
    """Export benchmark summary JSON."""
    from .policy import ProjectAccessPolicy
    policy = ProjectAccessPolicy(user=request.user)
    visible_projects = policy.visible_projects()
    extractions = TableExtraction.objects.filter(
        table_candidate__document__collection__in=visible_projects
    ).select_related("table_candidate", "extraction_run")
    reviews = Review.objects.filter(
        review_task__table_candidate__document__collection__in=visible_projects
    ).select_related("review_task__table_candidate")

    summary = {
        "exported_at": AuditEvent.objects.latest("created_at").created_at.isoformat() if AuditEvent.objects.exists() else "",
        "collection": "DP-Bench full tables",
        "total_tables": TableCandidate.objects.filter(
            document__collection__in=visible_projects
        ).count(),
        "total_reviews": reviews.count(),
        "profiles": {},
    }

    for profile in ["standard-docling", "granite-table-crop"]:
        p_extractions = extractions.filter(extraction_run__profile=profile, teds_s__isnull=False)
        if p_extractions.exists():
            summary["profiles"][profile] = {
                "count": p_extractions.count(),
                "mean_teds_s": p_extractions.aggregate(Avg("teds_s"))["teds_s__avg"],
                "mean_teds": p_extractions.aggregate(Avg("teds"))["teds__avg"],
            }

    response = HttpResponse(content_type="application/json")
    response["Content-Disposition"] = 'attachment; filename="benchmark-summary.json"'
    response.write(json.dumps(summary, indent=2))

    log_audit(request, "export_created", "Export", "benchmark-summary.json")
    return response


# --- Guidelines ---

@login_required
def guidelines(request):
    return render(request, "workbench/guidelines.html")


# --- User Settings ---

@login_required
def user_settings(request):
    """User profile and language settings page."""
    from django.utils.crypto import get_random_string
    from workbench.models import UserPreferences, ApiToken
    

    prefs = UserPreferences.get_or_create_for_user(request.user)

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "update_preferences":
            submitted_language = request.POST.get("ui_language", prefs.ui_language)
            valid_languages = {code for code, _label in LANGUAGES}
            if submitted_language in valid_languages:
                prefs.ui_language = submitted_language
            prefs.timezone = request.POST.get("timezone", prefs.timezone)
            prefs.guided_explanations = "guided_explanations" in request.POST
            prefs.save()
            from django.contrib import messages
            messages.success(request, _("Preferences saved."))

        elif action == "create_api_token":
            token_name = request.POST.get("token_name", "API Token").strip()
            if token_name:
                import secrets
                raw_token = secrets.token_urlsafe(36)
                ApiToken.objects.create(
                    user=request.user,
                    name=token_name,
                    token_prefix=raw_token[:8],
                    token_hash=ApiToken.hash_token(raw_token),
                    scopes=["projects:read", "documents:read", "tasks:read",
                            "documents:upload", "jobs:submit", "jobs:read",
                            "reviews:write", "statistics:read",
                            "chat:read", "chat:write", "chat:retry",
                            "diagnostics:read", "support:read", "support:export"],
                )
                from django.contrib import messages
                messages.success(
                    request,
                    _("Token created: %(token)s. Copy it now — it won't be shown again.") % {"token": raw_token},
                )

        elif action == "revoke_api_token":
            token_id = request.POST.get("token_id")
            token = ApiToken.objects.filter(user=request.user, pk=token_id).first()
            if token:
                from django.utils import timezone
                token.revoked_at = timezone.now()
                token.save(update_fields=["revoked_at"])
                from django.contrib import messages
                messages.success(request, _("Token revoked."))
            else:
                from django.contrib import messages
                messages.error(request, _("Token not found."))
        elif action == "change_password":
            old_password = request.POST.get("old_password", "")
            new_password = request.POST.get("new_password", "")
            confirm_password = request.POST.get("confirm_password", "")

            if not request.user.check_password(old_password):
                from django.contrib import messages
                messages.error(request, _("Current password is incorrect."))
            elif new_password != confirm_password:
                from django.contrib import messages
                messages.error(request, _("New passwords do not match."))
            elif len(new_password) < 8:
                from django.contrib import messages
                messages.error(request, _("New password must be at least 8 characters."))
            else:
                from django.contrib.auth import update_session_auth_hash
                request.user.set_password(new_password)
                request.user.save()
                update_session_auth_hash(request, request.user)
                from .models import UserPreferences
                UserPreferences.get_or_create_for_user(request.user)
                request.user.preferences.must_change_password = False
                request.user.preferences.save(update_fields=["must_change_password"])
                from django.contrib import messages
                messages.success(request, _("Password changed."))

        return redirect("user_settings")

    # Get user's existing tokens
    tokens = ApiToken.objects.filter(user=request.user).order_by("-created_at")
    from django.utils import timezone
    now = timezone.now()

    # Timezone choices as (code, display_name) tuples
    timezone_choices = [
        ("UTC", "UTC"),
        ("Europe/Vienna", "Vienna (CET/CEST)"),
        ("Europe/Berlin", "Berlin (CET/CEST)"),
        ("Europe/Zurich", "Zurich (CET/CEST)"),
        ("America/New_York", "New York (EST/EDT)"),
    ]

    return render(request, "workbench/user_settings.html", {
        "prefs": prefs,
        "tokens": tokens,
        "now": now,
        "language_choices": LANGUAGES,
        "timezone_choices": timezone_choices,
    })


# --- Static assets ---

def serve_htmx(request):
    """Serve self-hosted HTMX."""
    htmx_path = Path(settings.BASE_DIR) / "static" / "htmx.min.js"
    if htmx_path.exists():
        return HttpResponse(htmx_path.read_text(), content_type="application/javascript")
    # Fallback to CDN
    return redirect("https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js")


# --- Upload / Processing ---


@login_required
def forced_password_change(request):
    """Standalone forced password-change page (for must_change_password users)."""
    from .models import UserPreferences

    if request.method == "POST":
        current = request.POST.get("current_password", "")
        new_password = request.POST.get("new_password", "")
        confirm = request.POST.get("confirm_password", "")
        if not request.user.check_password(current):
            messages.error(request, _("Current password is incorrect."))
        elif new_password != confirm:
            messages.error(request, _("New passwords do not match."))
        elif len(new_password) < 8:
            messages.error(request, _("New password must be at least 8 characters."))
        elif request.user.check_password(new_password):
            messages.error(request, _("New password must differ from the current one."))
        else:
            from django.contrib.auth import update_session_auth_hash
            request.user.set_password(new_password)
            request.user.save(update_fields=["password"])
            update_session_auth_hash(request, request.user)
            prefs = UserPreferences.get_or_create_for_user(request.user)
            prefs.must_change_password = False
            prefs.save(update_fields=["must_change_password"])
            messages.success(request, _("Password updated. You can now continue."))
            return redirect(request.GET.get("next") or "/")

    return render(request, "workbench/forced_password_change.html", {
        "username": request.user.get_username(),
        "next": request.GET.get("next", "/"),
    })


@login_required
def document_new(request, project_id=None):
    """Choose an editable project, upload a PDF, and start processing."""
    from .policy import ProjectAccessPolicy
    from .services import DocumentIngestionService
    from .models import ProcessingPreset

    policy = ProjectAccessPolicy(user=request.user)
    editable_projects = [
        project for project in policy.visible_projects()
        if policy.can_edit(project)
    ]
    editable_project_ids = {project.pk for project in editable_projects}

    requested_project_id = project_id or request.POST.get("project") or request.GET.get("project")
    if not requested_project_id and len(editable_projects) == 1:
        requested_project_id = editable_projects[0].pk

    collection = None
    if requested_project_id:
        try:
            requested_project_id = int(requested_project_id)
        except (TypeError, ValueError):
            requested_project_id = None
        if requested_project_id in editable_project_ids:
            collection = next(
                project for project in editable_projects if project.pk == requested_project_id
            )

    for project in editable_projects:
        project.is_selected = collection is not None and project.pk == collection.pk

    presets = ProcessingPreset.objects.filter(is_active=True)
    standard_preset = presets.filter(slug="quick-extraction").first() or presets.first()
    advanced_presets = presets.exclude(pk=standard_preset.pk) if standard_preset else presets.none()
    selected_preset_slug = request.POST.get(
        "preset",
        standard_preset.slug if standard_preset else "",
    )

    if request.method == "POST":
        if collection is None:
            messages.error(request, _("Choose a project you can edit."))
        else:
            uploaded_files = request.FILES.getlist("file")
            if not uploaded_files:
                messages.error(request, _("Choose a PDF or image to upload."))
            else:
                service = DocumentIngestionService(user=request.user, policy=policy)
                jobs, errors = service.create_uploads(
                    project=collection,
                    uploaded_files=uploaded_files,
                    preset_slug=selected_preset_slug,
                )
                for job in jobs:
                    log_audit(request, "document_uploaded", "SourceDocument", job.source_document_id)
                # Surface per-file failures without aborting the good ones.
                for err in errors[:10]:
                    messages.error(request, _("{filename}: {error}").format(
                        filename=err["filename"], error=err["error"]))
                if len(jobs) == 1:
                    messages.success(
                        request,
                        _("Upload received. The document has been queued for analysis."),
                    )
                    return redirect("job_status", job_id=jobs[0].pk)
                if len(jobs) > 1:
                    messages.success(
                        request,
                        _("Upload received. {count} documents have been queued for analysis.").format(
                            count=len(jobs)),
                    )
                    return redirect("collection_detail", collection_id=collection.pk)
                # Every file failed: no redirect; re-render the form with the
                # error messages so the user can retry.

    upload_max_bytes = settings.DSW_UPLOAD_MAX_FILE_SIZE_BYTES
    return render(request, "workbench/document_new.html", {
        "editable_projects": editable_projects,
        "selected_project": collection,
        "standard_preset": standard_preset,
        "advanced_presets": advanced_presets,
        "selected_preset_slug": selected_preset_slug,
        "upload_max_bytes": upload_max_bytes,
        "upload_max_mb": upload_max_bytes // (1024 * 1024),
        "upload_max_files": settings.DSW_UPLOAD_MAX_FILES_PER_REQUEST,
    })


@login_required
def project_process(request, project_id):
    """Compatibility route for old project-specific upload links."""
    if request.method == "GET":
        return redirect(f"{reverse('document_new')}?project={project_id}")
    return document_new(request, project_id=project_id)


@login_required
@require_POST
def document_upload_file(request):
    """Single-file XHR upload endpoint for the multi-file drop zone.

    The drop zone posts each accepted file here, one request at a time, so a
    large batch becomes many small multipart requests instead of one giant one
    (which would tie up a worker and risk the Gunicorn timeout). Session + CSRF
    authenticated; returns JSON. Mirrors the bearer-authenticated API upload
    endpoint for the browser flow.
    """
    from .policy import ProjectAccessPolicy
    from .services import DocumentIngestionService

    def error(message, status=400):
        return JsonResponse({"error": str(message)}, status=status)

    project_id = request.POST.get("project")
    try:
        project = Collection.objects.get(pk=project_id)
    except (TypeError, ValueError, Collection.DoesNotExist):
        return error(_("Choose a project you can edit."))

    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_edit(project):
        return error(_("You do not have edit access to this project."), status=403)

    uploaded_files = request.FILES.getlist("file")
    if not uploaded_files:
        return error(_("Choose a PDF or image to upload."))

    preset_slug = request.POST.get("preset", "quick-extraction")
    service = DocumentIngestionService(user=request.user, policy=policy)
    jobs, errors = service.create_uploads(
        project=project,
        uploaded_files=uploaded_files,
        preset_slug=preset_slug,
    )

    collection_url = reverse("collection_detail", kwargs={"collection_id": project.pk})
    if not jobs:
        return JsonResponse(
            {"error": errors[0]["error"] if errors else str(_("Upload failed.")), "collection_url": collection_url},
            status=400,
        )

    job = jobs[0]
    log_audit(request, "document_uploaded", "SourceDocument", job.source_document_id)
    source_doc = job.source_document
    return JsonResponse({
        "status": "queued",
        "job_id": job.pk,
        "source_document_id": source_doc.pk,
        "filename": source_doc.filename,
        "sha256": source_doc.sha256,
        "job_status_url": reverse("job_status", kwargs={"job_id": job.pk}),
        "collection_url": collection_url,
    }, status=201)


@login_required
def job_status(request, job_id):
    """Show processing job status with HTMX polling."""
    from .policy import ProjectAccessPolicy
    from .models import ProcessingJob

    job = get_object_or_404(ProcessingJob, pk=job_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_job(job):
        messages.error(request, _("You do not have access to this job."))
        return redirect("dashboard")

    context = {
        "job": job,
        "source_document": job.source_document,
    }

    # HTMX partial refresh (exactly one replaceable root block)
    if request.headers.get("HX-Request"):
        return render(request, "workbench/_job_status_block.html", context)

    return render(request, "workbench/job_status.html", context)


@login_required
@require_POST
def job_recovery_action(request, job_id):
    """Resume an interrupted job or explicitly close an uncertain job."""
    from .policy import ProjectAccessPolicy
    from .models import ProcessingJob

    job = get_object_or_404(ProcessingJob, pk=job_id)
    if not ProjectAccessPolicy(user=request.user).can_access_job(job):
        messages.error(request, _("You do not have access to this processing job."))
        return redirect("dashboard")

    action = request.POST.get("action")
    if action == "mark_failed" and job.state == "submission_uncertain":
        job.transition_to("failed")
        job.error_message = _("The uncertain processor submission was closed by an operator.")
        job.status_message = _("Marked failed. Upload the document again to retry.")
        job.save(update_fields=["error_message", "status_message"])
        messages.warning(request, _("The uncertain job was marked failed."))
    elif action == "retry_new" and job.state in {"submission_uncertain", "interrupted", "partial", "failed", "cancelled"}:
        from .services import DocumentIngestionService, IngestionError
        try:
            retry = DocumentIngestionService(user=request.user).retry_existing(job=job)
        except IngestionError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, _("A new processing attempt was queued."))
            return redirect("job_status", job_id=retry.id)
    elif job.state == "interrupted" and job.external_job_id:
        target = "importing" if action == "retry_import" else "processing"
        job.transition_to(target)
        job.error_message = ""
        job.status_message = _("Recovery requested; the worker will continue this job.")
        job.save(update_fields=["error_message", "status_message"])
        messages.success(request, _("Recovery requested."))
    else:
        messages.error(request, _("This job cannot be resumed from its current state."))
    return redirect("job_status", job_id=job.id)


# --- Secure artifact serving ---

def _resolve_artifact_path(relative_path: str) -> Path:
    """Resolve and validate an artifact file path.

    Uses explicit Boolean containment check.
    Raises Http404 if path escapes the artifacts base directory.
    """
    from django.http import Http404

    artifacts_base = Path(getattr(settings, "ARTIFACTS_BASE_DIR", "/var/lib/dsw/artifacts"))
    file_path = artifacts_base / relative_path

    base = artifacts_base.resolve()
    resolved = file_path.resolve()

    if not resolved.is_relative_to(base):
        raise Http404("Invalid file path.")

    if not resolved.exists():
        raise Http404("File not found on disk.")

    return resolved


@login_required
def page_image(request, page_id):
    """Serve a page image with permission check."""
    from .policy import ProjectAccessPolicy
    from django.http import FileResponse, Http404

    page = get_object_or_404(Page, pk=page_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_view(page.document.collection):
        messages.error(request, _("You do not have access to this document."))
        return redirect("document_list")

    if not page.image_path:
        raise Http404("No image available for this page.")

    resolved = _resolve_artifact_path(page.image_path)
    return FileResponse(open(resolved, "rb"), content_type="image/png")


@login_required
def table_crop(request, table_id):
    """Serve a table crop image with permission check."""
    from .policy import ProjectAccessPolicy
    from django.http import FileResponse, Http404

    table = get_object_or_404(TableCandidate, pk=table_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_view(table.document.collection):
        messages.error(request, _("You do not have access to this document."))
        return redirect("document_list")

    if not table.crop_path:
        raise Http404("No crop available for this table.")

    resolved = _resolve_artifact_path(table.crop_path)
    return FileResponse(open(resolved, "rb"), content_type="image/png")


@login_required
def artifact_content(request, artifact_id):
    """Serve a processing artifact with permission check."""
    from .policy import ProjectAccessPolicy
    from .models import ProcessingArtifact
    from django.http import FileResponse, Http404, JsonResponse

    artifact = get_object_or_404(ProcessingArtifact, pk=artifact_id)
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_access_job(artifact.job):
        messages.error(request, _("You do not have access to this artifact."))
        return redirect("dashboard")

    # If it's JSON data, return directly
    if artifact.data and not artifact.file_path:
        return JsonResponse(artifact.data)

    if not artifact.file_path:
        raise Http404("No file for this artifact.")

    resolved = _resolve_artifact_path(artifact.file_path)

    # Determine content type
    content_type = "application/octet-stream"
    if resolved.suffix == ".json":
        content_type = "application/json"
    elif resolved.suffix in (".png", ".jpg", ".jpeg"):
        content_type = f"image/{resolved.suffix.lstrip('.')}"
    elif resolved.suffix == ".html":
        # Never execute imported HTML in the same origin as the workbench.
        content_type = "application/octet-stream"
    elif resolved.suffix == ".txt":
        content_type = "text/plain"

    response = FileResponse(open(resolved, "rb"), content_type=content_type)
    if resolved.suffix == ".html":
        response["Content-Disposition"] = f'attachment; filename="{resolved.name}"'
    return response
