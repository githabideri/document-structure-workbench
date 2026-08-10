"""URL configuration for Document Structure Workbench."""
from django.contrib import admin
from django.urls import path
from workbench import api, security, views

urlpatterns = [
    path("admin/", admin.site.urls),

    # Auth
    path("login/", security.ThrottledLoginView.as_view(template_name="auth/login.html"), name="login"),
    path("logout/", views.logout_view, name="logout"),

    # Dashboard
    path("", views.dashboard, name="dashboard"),

    # Collections (legacy, kept for DP-Bench review)
    path("collections/", views.collection_list, name="collection_list"),
    path("collections/<int:collection_id>/", views.collection_detail, name="collection_detail"),
    path("collections/new/", views.project_create, name="project_create"),
    path("collections/<int:collection_id>/archive/", views.project_archive, name="project_archive"),

    # Documents
    path("documents/", views.document_list, name="document_list"),
    path("search/", views.search_view, name="search"),
    path("search/reader/<int:passage_id>/", views.search_reader, name="search_reader"),
    path("chat/", views.chat_view, name="chat"),
    path("chat/<int:thread_id>/", views.chat_thread_view, name="chat_thread"),
    path("chat/<int:thread_id>/rename/", views.chat_thread_rename, name="chat_thread_rename"),
    path("chat/<int:thread_id>/archive/", views.chat_thread_archive, name="chat_thread_archive"),
    path("chat/runs/<int:run_id>/status/", views.chat_run_status, name="chat_run_status"),
    path("chat/runs/<int:run_id>/evidence/<str:marker>/", views.chat_evidence_detail, name="chat_evidence_detail"),
    path("chat/runs/<int:run_id>/diagnostics/", views.chat_run_diagnostics, name="chat_run_diagnostics"),
    path("chat/runs/<int:run_id>/diagnostics/data/", views.chat_run_diagnostics_data, name="chat_run_diagnostics_data"),
    path("documents/new/", views.document_new, name="document_new"),
    path("documents/<int:document_id>/", views.document_detail, name="document_detail"),
    path("regions/<int:region_id>/correct-text/", views.correct_region_text, name="correct_region_text"),
    path("documents/<int:document_id>/ocr-history/", views.ocr_history_fragment, name="ocr_history_fragment"),
    path("regions/<int:region_id>/inspector/", views.region_inspector, name="region_inspector"),
    path("regions/<int:region_id>/versions/", views.region_versions_fragment, name="region_versions_fragment"),
    path("regions/<int:region_id>/ocr/", views.create_ocr_request, name="create_ocr_request"),
    path("regions/<int:region_id>/htr/", views.create_region_htr, name="create_region_htr"),
    path("pages/<int:page_id>/workspace-data/", views.page_workspace_data, name="page_workspace_data"),
    path("pages/<int:page_id>/ocr/", views.create_page_ocr_request, name="create_page_ocr_request"),
    path("ocr/requests/<int:request_id>/accept/", views.accept_ocr_request, name="accept_ocr_request"),
    path("htr/runs/<int:request_id>/accept/", views.accept_region_htr, name="accept_region_htr"),
    path("regions/<int:region_id>/correct/", views.correct_region, name="correct_region"),
    path("corrections/<int:correction_id>/revert/", views.revert_region_correction, name="revert_region_correction"),

    # Help
    path("help/", views.help_page, name="help"),

    # Upload / Processing
    path("uploads/file/", views.document_upload_file, name="document_upload_file"),
    path("projects/<int:project_id>/process/", views.project_process, name="project_process"),
    path("jobs/<int:job_id>/", views.job_status, name="job_status"),
    path("jobs/<int:job_id>/recovery/", views.job_recovery_action, name="job_recovery_action"),
    path("uploads/<int:source_id>/archive/", views.source_archive, name="source_archive"),

    # Secure artifact serving
    path("pages/<int:page_id>/image/", views.page_image, name="page_image"),
    path("tables/<int:table_id>/crop/", views.table_crop, name="table_crop"),
    path("artifacts/<int:artifact_id>/content/", views.artifact_content, name="artifact_content"),

    # Document / project text export (plain text / Markdown)
    path("export/revisions/<int:revision_id>/", views.export_revision, name="export_revision"),
    path("export/projects/<int:project_id>/", views.export_project, name="export_project"),

    # Reviews
    path("reviews/", views.review_list, name="review_list"),
    path("reviews/next/", views.review_next, name="review_next"),
    path("reviews/<int:task_id>/", views.review_detail, name="review_detail"),
    path("reviews/<int:task_id>/submit/", views.review_submit, name="review_submit"),
    path("reviews/<int:task_id>/reveal/", views.review_reveal, name="review_reveal"),
    path("reviews/<int:task_id>/post-reveal/", views.review_post_reveal, name="review_post_reveal"),
    path("reviews/<int:task_id>/skip/", views.review_skip, name="review_skip"),
    path("reviews/<int:task_id>/needs-expert/", views.review_needs_expert, name="review_needs_expert"),

    # Decisions
    path("decisions/", views.decision_list, name="decision_list"),
    path("decisions/<int:table_id>/", views.decision_detail, name="decision_detail"),
    path("decisions/<int:table_id>/submit/", views.decision_submit, name="decision_submit"),

    # Curator
    path("curator/", views.curator_dashboard, name="curator_dashboard"),
    path("curator/technical/", views.technical_report, name="technical_report"),
    path("curator/export/reviews/", views.export_reviews, name="export_reviews"),
    path("curator/export/decisions/", views.export_decisions, name="export_decisions"),
    path("curator/export/summary/", views.export_summary, name="export_summary"),

    # Guidelines
    path("guidelines/", views.guidelines, name="guidelines"),

    # User Settings
    path("settings/", views.user_settings, name="user_settings"),
    path("settings/password/forced/", views.forced_password_change, name="forced_password_change"),

    # REST API v1 (secure, token-authenticated)
    path("api/v1/health/", api.api_health, name="api_health"),
    path("api/v1/me/", api.api_me, name="api_me"),
    path("api/v1/projects/", api.api_projects, name="api_projects"),
    path("api/v1/projects/<int:project_id>/", api.api_project_detail, name="api_project_detail"),
    path("api/v1/projects/<int:project_id>/archive/", api.api_project_archive, name="api_project_archive"),
    path("api/v1/projects/<int:project_id>/documents/", api.api_documents, name="api_documents"),
    path("api/v1/projects/<int:project_id>/export/", api.api_project_export, name="api_project_export"),
    path("api/v1/documents/", api.api_documents_all, name="api_documents_all"),
    path("api/v1/documents/<int:document_id>/", api.api_document_detail, name="api_document_detail"),
    path("api/v1/documents/<int:document_id>/revisions/", api.api_document_revisions, name="api_document_revisions"),
    path("api/v1/documents/<int:document_id>/revisions/<int:revision_id>/", api.api_revision_detail, name="api_revision_detail"),
    path("api/v1/documents/<int:document_id>/revisions/<int:revision_id>/pages/", api.api_revision_pages, name="api_revision_pages"),
    path("api/v1/documents/<int:document_id>/revisions/<int:revision_id>/export/", api.api_revision_export, name="api_revision_export"),
    path("api/v1/documents/<int:document_id>/revisions/<int:revision_id>/pages/<int:page_number>/", api.api_revision_page, name="api_revision_page"),
    path("api/v1/pages/<int:page_id>/regions/", api.api_page_regions, name="api_page_regions"),
    path("api/v1/pages/<int:page_id>/image/", api.api_page_image, name="api_page_image"),
    path("api/v1/regions/<int:region_id>/", api.api_region_detail, name="api_region_detail"),
    path("api/v1/regions/<int:region_id>/corrections/", api.api_region_corrections, name="api_region_corrections"),
    path("api/v1/search/", api.api_search, name="api_search"),
    path("api/v1/ui-diagnostics/", api.api_ui_diagnostics, name="api_ui_diagnostics"),
    path("api/v1/projects/<int:project_id>/upload/", api.api_upload_document, name="api_upload_document"),
    path("api/v1/documents/<int:document_id>/archive/", api.api_document_archive, name="api_document_archive"),
    path("api/v1/presets/", api.api_presets, name="api_presets"),
    path("api/v1/jobs/<int:job_id>/", api.api_job_detail, name="api_job_detail"),
    path("api/v1/jobs/<int:job_id>/recovery/", api.api_job_recovery, name="api_job_recovery"),
    path("api/v1/regions/<int:region_id>/ocr/", api.api_region_ocr, name="api_region_ocr"),
    path("api/v1/regions/<int:region_id>/text-corrections/", api.api_region_text_correction, name="api_region_text_correction"),
    path("api/v1/regions/<int:region_id>/type-corrections/", api.api_region_type_correction, name="api_region_type_correction"),
    path("api/v1/regions/<int:region_id>/suppression-corrections/", api.api_region_suppression_correction, name="api_region_suppression_correction"),
    path("api/v1/corrections/<int:correction_id>/revert/", api.api_correction_revert, name="api_correction_revert"),
    path("api/v1/pages/<int:page_id>/ocr/", api.api_page_ocr, name="api_page_ocr"),
    path("api/v1/ocr/requests/<int:request_id>/", api.api_ocr_request_detail, name="api_ocr_request_detail"),
    path("api/v1/ocr/requests/<int:request_id>/accept/", api.api_ocr_request_accept, name="api_ocr_request_accept"),
    # Handwritten-text recognition (HTR) — region-scoped rerun, session JSON.
    path("api/regions/<int:region_id>/htr-runs/", api.api_region_htr_runs, name="api_region_htr_runs"),
    path("api/htr-runs/<int:request_id>/", api.api_htr_run_detail, name="api_htr_run_detail"),
    path("api/htr-runs/<int:request_id>/accept/", api.api_htr_run_accept, name="api_htr_run_accept"),
    path("api/v1/tasks/", api.api_tasks, name="api_tasks"),
    path("api/v1/tasks/<int:task_id>/", api.api_task_detail, name="api_task_detail"),
    path("api/v1/tasks/<int:task_id>/submit/", api.api_task_submit, name="api_task_submit"),
    path("api/v1/statistics/", api.api_statistics, name="api_statistics"),
    path("api/v1/chat/threads/", api.api_chat_threads, name="api_chat_threads"),
    path("api/v1/chat/threads/list/", api.api_chat_threads_list, name="api_chat_threads_list"),
    path("api/v1/chat/threads/<int:thread_id>/", api.api_chat_thread_detail, name="api_chat_thread_detail"),
    path("api/v1/chat/threads/<int:thread_id>/rename/", api.api_chat_thread_rename, name="api_chat_thread_rename"),
    path("api/v1/chat/threads/<int:thread_id>/archive/", api.api_chat_thread_archive, name="api_chat_thread_archive"),
    path("api/v1/chat/threads/<int:thread_id>/runs/", api.api_chat_thread_runs, name="api_chat_thread_runs"),
    path("api/v1/chat/runs/<int:run_id>/", api.api_chat_run_detail, name="api_chat_run_detail"),
    path("api/v1/chat/runs/<int:run_id>/evidence/", api.api_chat_run_evidence, name="api_chat_run_evidence"),
    path("api/v1/chat/runs/<int:run_id>/diagnostics/", api.api_chat_run_diagnostics, name="api_chat_run_diagnostics"),
    path("api/v1/chat/runs/<int:run_id>/retry/", api.api_chat_run_retry, name="api_chat_run_retry"),
    path("api/v1/chat/runs/<int:run_id>/support-bundle/", api.api_chat_support_bundle, name="api_chat_support_bundle"),
    path("api/v1/support-bundles/<int:bundle_id>/", api.api_support_bundle_detail, name="api_support_bundle_detail"),

    # Static assets
    # Keep this outside STATIC_URL so Django's development static handler does
    # not intercept the self-hosted/CDN fallback view.
    path("assets/htmx.min.js", views.serve_htmx, name="serve_htmx"),
]
