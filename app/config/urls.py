"""URL configuration for Document Structure Workbench."""
from django.contrib import admin
from django.urls import path
from django.contrib.auth import views as auth_views
from workbench import views
from workbench import api

urlpatterns = [
    path("admin/", admin.site.urls),

    # Auth
    path("login/", auth_views.LoginView.as_view(template_name="auth/login.html"), name="login"),
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
    path("chat/", views.chat_view, name="chat"),
    path("chat/<int:thread_id>/", views.chat_thread_view, name="chat_thread"),
    path("chat/<int:thread_id>/rename/", views.chat_thread_rename, name="chat_thread_rename"),
    path("chat/<int:thread_id>/archive/", views.chat_thread_archive, name="chat_thread_archive"),
    path("chat/runs/<int:run_id>/status/", views.chat_run_status, name="chat_run_status"),
    path("chat/runs/<int:run_id>/diagnostics/", views.chat_run_diagnostics, name="chat_run_diagnostics"),
    path("documents/new/", views.document_new, name="document_new"),
    path("documents/<int:document_id>/", views.document_detail, name="document_detail"),
    path("regions/<int:region_id>/correct-text/", views.correct_region_text, name="correct_region_text"),
    path("regions/<int:region_id>/ocr/", views.create_ocr_request, name="create_ocr_request"),
    path("pages/<int:page_id>/ocr/", views.create_page_ocr_request, name="create_page_ocr_request"),
    path("ocr/requests/<int:request_id>/accept/", views.accept_ocr_request, name="accept_ocr_request"),
    path("regions/<int:region_id>/correct/", views.correct_region, name="correct_region"),
    path("corrections/<int:correction_id>/revert/", views.revert_region_correction, name="revert_region_correction"),

    # Help
    path("help/", views.help_page, name="help"),

    # Upload / Processing
    path("projects/<int:project_id>/process/", views.project_process, name="project_process"),
    path("jobs/<int:job_id>/", views.job_status, name="job_status"),
    path("jobs/<int:job_id>/recovery/", views.job_recovery_action, name="job_recovery_action"),
    path("uploads/<int:source_id>/archive/", views.source_archive, name="source_archive"),

    # Secure artifact serving
    path("pages/<int:page_id>/image/", views.page_image, name="page_image"),
    path("tables/<int:table_id>/crop/", views.table_crop, name="table_crop"),
    path("artifacts/<int:artifact_id>/content/", views.artifact_content, name="artifact_content"),

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

    # REST API v1 (secure, token-authenticated)
    path("api/v1/health/", api.api_health, name="api_health"),
    path("api/v1/me/", api.api_me, name="api_me"),
    path("api/v1/projects/", api.api_projects, name="api_projects"),
    path("api/v1/projects/<int:project_id>/", api.api_project_detail, name="api_project_detail"),
    path("api/v1/projects/<int:project_id>/documents/", api.api_documents, name="api_documents"),
    path("api/v1/projects/<int:project_id>/upload/", api.api_upload_document, name="api_upload_document"),
    path("api/v1/presets/", api.api_presets, name="api_presets"),
    path("api/v1/jobs/<int:job_id>/", api.api_job_detail, name="api_job_detail"),
    path("api/v1/jobs/<int:job_id>/recovery/", api.api_job_recovery, name="api_job_recovery"),
    path("api/v1/regions/<int:region_id>/ocr/", api.api_region_ocr, name="api_region_ocr"),
    path("api/v1/pages/<int:page_id>/ocr/", api.api_page_ocr, name="api_page_ocr"),
    path("api/v1/ocr/requests/<int:request_id>/", api.api_ocr_request_detail, name="api_ocr_request_detail"),
    path("api/v1/ocr/requests/<int:request_id>/accept/", api.api_ocr_request_accept, name="api_ocr_request_accept"),
    path("api/v1/tasks/", api.api_tasks, name="api_tasks"),
    path("api/v1/tasks/<int:task_id>/", api.api_task_detail, name="api_task_detail"),
    path("api/v1/tasks/<int:task_id>/submit/", api.api_task_submit, name="api_task_submit"),
    path("api/v1/statistics/", api.api_statistics, name="api_statistics"),
    path("api/v1/chat/threads/", api.api_chat_threads, name="api_chat_threads"),
    path("api/v1/chat/threads/list/", api.api_chat_threads_list, name="api_chat_threads_list"),
    path("api/v1/chat/threads/<int:thread_id>/", api.api_chat_thread_detail, name="api_chat_thread_detail"),
    path("api/v1/chat/threads/<int:thread_id>/runs/", api.api_chat_thread_runs, name="api_chat_thread_runs"),
    path("api/v1/chat/runs/<int:run_id>/", api.api_chat_run_detail, name="api_chat_run_detail"),
    path("api/v1/chat/runs/<int:run_id>/evidence/", api.api_chat_run_evidence, name="api_chat_run_evidence"),
    path("api/v1/chat/runs/<int:run_id>/retry/", api.api_chat_run_retry, name="api_chat_run_retry"),
    path("api/v1/chat/runs/<int:run_id>/support-bundle/", api.api_chat_support_bundle, name="api_chat_support_bundle"),
    path("api/v1/support-bundles/<int:bundle_id>/", api.api_support_bundle_detail, name="api_support_bundle_detail"),

    # Static assets
    # Keep this outside STATIC_URL so Django's development static handler does
    # not intercept the self-hosted/CDN fallback view.
    path("assets/htmx.min.js", views.serve_htmx, name="serve_htmx"),
]
