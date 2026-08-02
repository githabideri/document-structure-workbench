"""URL configuration for Archive Structure Workbench."""
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

    # Collections
    path("collections/", views.collection_list, name="collection_list"),
    path("collections/<int:collection_id>/", views.collection_detail, name="collection_detail"),

    # Documents
    path("documents/", views.document_list, name="document_list"),
    path("documents/<int:document_id>/", views.document_detail, name="document_detail"),

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

    # REST API
    path("api/v1/health/", api.api_health, name="api_health"),
    path("api/status/", api.api_status, name="api_status"),
    path("api/collections/", api.api_collections, name="api_collections"),
    path("api/collections/<int:collection_id>/", api.api_collection_detail, name="api_collection_detail"),
    path("api/tasks/", api.api_tasks, name="api_tasks"),
    path("api/tasks/<int:task_id>/", api.api_task_detail, name="api_task_detail"),
    path("api/tasks/<int:task_id>/submit/", api.api_task_submit, name="api_task_submit"),
    path("api/tasks/<int:task_id>/skip/", api.api_task_skip, name="api_task_skip"),
    path("api/tasks/<int:task_id>/flag-expert/", api.api_task_flag_expert, name="api_task_flag_expert"),
    path("api/statistics/", api.api_statistics, name="api_statistics"),

    # Static assets
    path("static/htmx.min.js", views.serve_htmx, name="serve_htmx"),
]
