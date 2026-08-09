"""
Project access policy — centralized authorization for all views and API.

Usage:
    from workbench.policy import ProjectAccessPolicy

    # In views:
    policy = ProjectAccessPolicy(user=request.user)
    if not policy.can_view(collection):
        return redirect("collection_list")

    # In API:
    policy = ProjectAccessPolicy(token=api_token)
    if not policy.can_edit(collection):
        return JsonResponse({"error": "Access denied"}, status=403)

Design:
    - Global administrators bypass all project membership checks.
    - A user must have a ProjectMembership row to access a project.
    - Service accounts are scoped to their token's project (if any).
    - Unscoped service accounts see nothing unless globally admin (deliberate).
"""
from django.conf import settings
from django.contrib.auth import get_user_model

User = get_user_model()


class ProjectAccessPolicy:
    """Centralized project access checks.

    Accepts either a User (session auth) or an ApiToken (bearer auth).
    """

    def __init__(self, user=None, token=None):
        self.user = user
        self.token = token
        self._is_global_admin = None

    # ------------------------------------------------------------------
    # Global admin check
    # ------------------------------------------------------------------

    def _check_global_admin(self):
        """Cache whether this identity is a global administrator."""
        if self._is_global_admin is not None:
            return self._is_global_admin

        if self.user:
            self._is_global_admin = self.user.is_superuser or self.user.groups.filter(
                name="Administrator"
            ).exists()
        elif self.token and self.token.service_account:
            # Service accounts are never global admins by default.
            # They need explicit project membership or token scoping.
            self._is_global_admin = False
        else:
            self._is_global_admin = False

        return self._is_global_admin

    # ------------------------------------------------------------------
    # Shared-workspace mode
    # ------------------------------------------------------------------

    def _shared_workspace(self):
        """True when the deployment opts into a single shared workspace where
        every signed-in user can view and edit every non-archived project."""
        return getattr(settings, "DSW_PROJECT_VISIBILITY", "membership") == "all_users"

    # ------------------------------------------------------------------
    # Visible projects
    # ------------------------------------------------------------------

    def visible_projects(self):
        """Return a queryset of projects the identity can see."""
        from .models import Collection, ProjectMembership

        if self._check_global_admin():
            return Collection.objects.filter(is_archived=False)

        if self.user:
            if self._shared_workspace():
                return Collection.objects.filter(is_archived=False)
            return Collection.objects.filter(
                memberships__user=self.user, is_archived=False
            ).distinct()
        elif self.token:
            if self.token.user_id:
                return Collection.objects.filter(
                    memberships__user_id=self.token.user_id, is_archived=False
                ).distinct()
            if self.token.project_id:
                return Collection.objects.filter(pk=self.token.project_id, is_archived=False)
            # Unscoped service account: no projects by default
            return Collection.objects.none()

        return Collection.objects.none()

    # ------------------------------------------------------------------
    # Per-project checks
    # ------------------------------------------------------------------

    def can_view(self, project):
        """Can the identity view this project at all?"""
        if getattr(project, "is_archived", False):
            return False
        if self._check_global_admin():
            return True

        if self.user:
            if self._shared_workspace():
                # Archived projects were rejected above.
                return True
            return self._get_membership(project) is not None
        elif self.token:
            return self._check_token_access(project, min_role="viewer")
        return False

    def resolve_chat_scope(self, *, mode="project", project_id=None, source_ids=None,
                           revision_ids=None, filters=None):
        """Resolve and freeze a chat scope using this identity's current access.

        The returned primitive-only dictionary is safe to persist on a run. Every
        source and revision is checked against the same authorized project set.
        """
        from .models import SourceDocument
        visible = self.visible_projects()
        visible_ids = set(visible.values_list("id", flat=True))
        mode = mode if mode in {"project", "all"} else "project"
        if mode == "project":
            if project_id is None:
                raise ValueError("project_id is required for project scope")
            project_id = int(project_id)
            if project_id not in visible_ids:
                raise PermissionError("Project is not accessible.")
            project_ids = [project_id]
        else:
            project_ids = sorted(visible_ids)
        requested_sources = {int(value) for value in (source_ids or [])}
        # Attachments may come from any project the identity can view; the
        # selected project/all-project mode controls search results separately.
        sources = SourceDocument.objects.filter(
            id__in=requested_sources, collection_id__in=visible_ids, is_archived=False,
        ).select_related("collection")
        if sources.count() != len(requested_sources):
            raise PermissionError("One or more attached documents are not accessible.")
        if mode == "all" and not requested_sources:
            requested_sources = set(SourceDocument.objects.filter(
                collection_id__in=project_ids, is_archived=False,
            ).values_list("id", flat=True))
        elif mode == "project":
            # Project scope searches every accessible source in that project;
            # supplied source_ids are manual attachments, not a hidden filter.
            requested_sources |= set(SourceDocument.objects.filter(
                collection_id=project_id, is_archived=False,
            ).values_list("id", flat=True))
        if revision_ids is None:
            revisions = list(SourceDocument.objects.filter(id__in=requested_sources).values_list("active_document_id", flat=True))
            revision_ids = sorted({value for value in revisions if value})
        else:
            revision_ids = sorted({int(value) for value in revision_ids if value})
        return {
            "mode": mode, "project_ids": project_ids,
            "source_ids": sorted(requested_sources),
            "revision_ids": revision_ids,
            "attachment_ids": sorted({int(value) for value in (source_ids or [])}),
            "filters": filters or {},
        }

    def can_edit(self, project):
        """Can the identity edit this project (upload, modify)?"""
        if getattr(project, "is_archived", False):
            return False
        if self._check_global_admin():
            return True

        if self.user:
            if self._shared_workspace():
                # Archived projects were rejected above.
                return True
            membership = self._get_membership(project)
            return membership is not None and membership.can_edit
        elif self.token:
            return self._check_token_access(project, min_role="editor")
        return False

    def can_review(self, project):
        """Can the identity review tasks in this project?"""
        if self._check_global_admin():
            return True

        if self.user:
            membership = self._get_membership(project)
            return membership is not None and membership.can_review
        elif self.token:
            return self._check_token_access(project, min_role="reviewer")
        return False

    def can_curate(self, project):
        """Can the identity curate (make decisions, export) in this project?"""
        if self._check_global_admin():
            return True

        if self.user:
            membership = self._get_membership(project)
            return membership is not None and membership.is_owner
        elif self.token:
            return self._check_token_access(project, min_role="owner")
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_membership(self, project):
        """Get the user's membership in a project, or None."""
        from .models import ProjectMembership
        return ProjectMembership.objects.filter(
            project=project, user=self.user
        ).first()

    def _check_token_access(self, project, min_role="viewer"):
        """Check if a token can access the project at the given role level.

        Key rule: A token scoped to a project grants VIEWER access only.
        It does NOT automatically grant editor, reviewer, or owner authority.
        For elevated access, the token's underlying user must have a
        ProjectMembership row with the required role.

        Service accounts: ProjectMembership.user is a FK to User, not
        ServiceAccount. Service accounts have no membership model — they
        are authorized solely by token scope (viewer-only).
        """
        from .models import ProjectMembership

        # Human user token: check actual membership
        if self.token.user_id:
            membership = ProjectMembership.objects.filter(
                project=project, user_id=self.token.user_id
            ).first()
            if membership is None:
                # Token scoped to project grants viewer-only access
                if self.token.project_id == project.pk:
                    return min_role == "viewer"
                return False
            role_order = {"viewer": 0, "reviewer": 1, "editor": 2, "owner": 3}
            return role_order.get(membership.role, 0) >= role_order.get(min_role, 0)

        # Service account token: token scope grants viewer-only access
        # (no ProjectMembership for service accounts — user FK is to User)
        if self.token.service_account_id:
            if self.token.project_id == project.pk:
                return min_role == "viewer"
            return False

        return False

    # ------------------------------------------------------------------
    # Convenience: check access for a document/artifact/task
    # ------------------------------------------------------------------

    def can_access_document(self, document):
        """Check access via the document's collection."""
        return self.can_view(document.collection)

    def can_access_source_document(self, source_document):
        """Check access via the source document's collection."""
        if source_document.collection_id is None:
            return False
        return self.can_view(source_document.collection)

    def can_access_job(self, job):
        """Check access via the job's source document."""
        return self.can_access_source_document(job.source_document)

    def can_access_task(self, task):
        """Check access via the task's table candidate document."""
        return self.can_access_document(task.table_candidate.document)
