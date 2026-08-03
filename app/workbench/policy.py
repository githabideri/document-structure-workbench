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
            self._is_global_admin = self.user.groups.filter(
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
    # Visible projects
    # ------------------------------------------------------------------

    def visible_projects(self):
        """Return a queryset of projects the identity can see."""
        from .models import Collection, ProjectMembership

        if self._check_global_admin():
            return Collection.objects.all()

        if self.user:
            return Collection.objects.filter(
                memberships__user=self.user
            ).distinct()
        elif self.token:
            if self.token.project_id:
                return Collection.objects.filter(pk=self.token.project_id)
            # Unscoped service account: no projects by default
            return Collection.objects.none()

        return Collection.objects.none()

    # ------------------------------------------------------------------
    # Per-project checks
    # ------------------------------------------------------------------

    def can_view(self, project):
        """Can the identity view this project at all?"""
        if self._check_global_admin():
            return True

        if self.user:
            return self._get_membership(project) is not None
        elif self.token:
            return self._check_token_access(project, min_role="viewer")
        return False

    def can_edit(self, project):
        """Can the identity edit this project (upload, modify)?"""
        if self._check_global_admin():
            return True

        if self.user:
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
