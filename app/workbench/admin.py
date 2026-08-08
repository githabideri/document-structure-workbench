from django.contrib import admin
from .models import (
    Collection, Document, Page, TableCandidate,
    ExtractionRun, TableExtraction, ReviewTask, Review,
    Decision, AuditEvent, ProjectMembership,
)


class ProjectMembershipInline(admin.TabularInline):
    """Manage a project's member roles directly on the project page."""

    model = ProjectMembership
    extra = 0
    fk_name = "project"
    autocomplete_fields = ["user"]
    fields = ["user", "role", "invited_by", "joined_at"]
    readonly_fields = ["joined_at"]

    def has_add_permission(self, request, obj=None):
        # Allow adding members from the project page.
        return True


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    list_display = ["name", "source_type", "created_at", "is_archived"]
    list_filter = ["source_type", "is_archived"]
    search_fields = ["name", "description"]
    inlines = [ProjectMembershipInline]


@admin.register(ProjectMembership)
class ProjectMembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "project", "role", "joined_at"]
    list_filter = ["role", "project"]
    search_fields = ["user__username", "user__email", "project__name"]
    autocomplete_fields = ["user", "project"]
    readonly_fields = ["joined_at"]
    fields = ["user", "project", "role", "invited_by", "joined_at"]
    list_select_related = ["user", "project", "invited_by"]

    def get_search_results(self, request, queryset, search_term):
        # Usernames are case-insensitive in this deployment; keep default search.
        return super().get_search_results(request, queryset, search_term)


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ["external_id", "filename", "collection", "page_count", "is_archived"]
    list_filter = ["collection", "is_archived"]
    search_fields = ["external_id", "filename"]


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    list_display = ["document", "page_number", "width", "height"]


@admin.register(TableCandidate)
class TableCandidateAdmin(admin.ModelAdmin):
    list_display = ["document", "stable_table_id", "page"]
    list_filter = ["document__collection"]
    search_fields = ["stable_table_id", "document__external_id"]


@admin.register(ExtractionRun)
class ExtractionRunAdmin(admin.ModelAdmin):
    list_display = ["profile", "document", "status", "started_at", "processing_seconds"]
    list_filter = ["profile", "status"]


@admin.register(TableExtraction)
class TableExtractionAdmin(admin.ModelAdmin):
    list_display = ["table_candidate", "extraction_run", "rows", "columns", "status", "teds_s"]
    list_filter = ["extraction_run__profile", "status"]


@admin.register(ReviewTask)
class ReviewTaskAdmin(admin.ModelAdmin):
    list_display = ["table_candidate", "assigned_to", "state", "priority", "created_at"]
    list_filter = ["state"]
    search_fields = ["table_candidate__document__external_id"]


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ["review_task", "reviewer", "preferred_result", "confidence", "created_at"]
    list_filter = ["preferred_result", "confidence"]


@admin.register(Decision)
class DecisionAdmin(admin.ModelAdmin):
    list_display = ["table_candidate", "decision", "decided_by", "created_at"]
    list_filter = ["decision"]


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ["event_type", "actor", "object_type", "created_at"]
    list_filter = ["event_type"]
    search_fields = ["object_id", "event_type"]
    readonly_fields = ["id", "actor", "event_type", "object_type", "object_id", "before", "after", "request_id", "created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
