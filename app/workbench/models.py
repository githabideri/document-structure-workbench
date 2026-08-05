"""
Document Structure Workbench - Core Data Models.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import JSONField
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

User = get_user_model()

# Language codes used across the platform
LANGUAGES = [
    ("en", "English"),
    ("de", "Deutsch"),
]


class UserPreferences(models.Model):
    """Per-user UI preferences. One row per user."""

    id = models.AutoField(primary_key=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="preferences")
    ui_language = models.CharField(max_length=10, choices=LANGUAGES, default="en")
    timezone = models.CharField(max_length=50, default="Europe/Vienna")
    guided_explanations = models.BooleanField(default=True)
    onboarding_completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "User preferences"
        verbose_name_plural = "User preferences"

    def __str__(self):
        return f"Preferences for {self.user.get_username()}"

    def get_absolute_url(self):
        return reverse("user_settings")

    @classmethod
    def get_or_create_for_user(cls, user):
        """Get or create preferences, ensuring a row always exists."""
        obj, created = cls.objects.get_or_create(user=user)
        return obj


class Collection(models.Model):
    """A named set of documents for processing and review."""

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    source_type = models.CharField(
        max_length=50,
        choices=[
            ("benchmark", "Benchmark dataset"),
            ("corpus", "Real corpus"),
            ("integration", "Integration validation"),
        ],
        default="benchmark",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("collection_detail", kwargs={"collection_id": self.id})


class Document(models.Model):
    """A single document within a collection."""

    id = models.AutoField(primary_key=True)
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name="documents")
    external_id = models.CharField(max_length=200, db_index=True)
    filename = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64, db_index=True)
    page_count = models.PositiveIntegerField(default=0)
    source_path = models.CharField(max_length=1000, blank=True)
    metadata = JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["external_id"]

    def __str__(self):
        return f"{self.filename} ({self.external_id})"

    def get_absolute_url(self):
        return reverse("document_detail", kwargs={"document_id": self.id})


class Page(models.Model):
    """A single page within a document."""

    id = models.AutoField(primary_key=True)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="pages")
    page_number = models.PositiveIntegerField()
    image_path = models.CharField(max_length=1000, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        unique_together = ["document", "page_number"]
        ordering = ["page_number"]

    def __str__(self):
        return f"Page {self.page_number} of {self.document}"


class TableCandidate(models.Model):
    """A detected table within a document page."""

    id = models.AutoField(primary_key=True)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="tables")
    page = models.ForeignKey(Page, on_delete=models.CASCADE, null=True, blank=True)
    stable_table_id = models.CharField(max_length=100, db_index=True)
    bbox = JSONField(default=list, blank=True)
    crop_path = models.CharField(max_length=1000, blank=True)
    ground_truth_path = models.CharField(max_length=1000, blank=True)
    metadata = JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ["document", "stable_table_id"]
        ordering = ["page", "stable_table_id"]

    def __str__(self):
        return f"{self.document.external_id} / {self.stable_table_id}"

    def get_absolute_url(self):
        return reverse("review_detail", kwargs={"task_id": self.id})


class ExtractionRun(models.Model):
    """A single extraction run on one or more documents."""

    id = models.AutoField(primary_key=True)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, null=True, blank=True, related_name="runs")
    profile = models.CharField(
        max_length=50,
        choices=[
            ("standard-docling", "Standard Docling (TableFormer)"),
            ("granite-table-crop", "Granite Vision (crop)"),
            ("ground-truth", "Ground truth"),
        ],
    )
    software_versions = JSONField(default=dict, blank=True)
    configuration = JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("running", "Running"),
            ("completed", "Completed"),
            ("failed", "Failed"),
            ("partial", "Partial"),
        ],
        default="pending",
    )
    processing_seconds = models.FloatField(null=True, blank=True)
    peak_vram_gib = models.FloatField(null=True, blank=True)
    git_commit = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.profile} - {self.document or 'batch'}"


class TableExtraction(models.Model):
    """A single table extraction result."""

    id = models.AutoField(primary_key=True)
    table_candidate = models.ForeignKey(TableCandidate, on_delete=models.CASCADE, related_name="extractions")
    extraction_run = models.ForeignKey(ExtractionRun, on_delete=models.CASCADE, related_name="extractions")
    rows = models.PositiveIntegerField(default=0)
    columns = models.PositiveIntegerField(default=0)
    raw_otsl = models.TextField(blank=True)
    raw_html = models.TextField(blank=True)
    normalized_cells = JSONField(default=list, blank=True)
    validation = JSONField(default=dict, blank=True)
    artifact_paths = JSONField(default=dict, blank=True)
    teds = models.FloatField(null=True, blank=True)
    teds_s = models.FloatField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=[
            ("success", "Success"),
            ("failed", "Failed"),
            ("skipped", "Skipped"),
            ("missing", "Missing"),
        ],
        default="success",
    )

    class Meta:
        ordering = ["table_candidate"]
        unique_together = ["table_candidate", "extraction_run"]

    def __str__(self):
        return f"Extraction of {self.table_candidate}"


class ReviewTask(models.Model):
    """A task to review a table."""

    STATE_CHOICES = [
        ("unassigned", "Unassigned"),
        ("assigned", "Assigned"),
        ("in_progress", "In Progress"),
        ("completed", "Completed"),
        ("needs_expert", "Needs Expert"),
        ("skipped", "Skipped"),
    ]

    id = models.AutoField(primary_key=True)
    table_candidate = models.OneToOneField(TableCandidate, on_delete=models.CASCADE, related_name="review_task")
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="review_tasks")
    state = models.CharField(max_length=20, choices=STATE_CHOICES, default="unassigned")
    priority = models.PositiveIntegerField(default=50)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Stable candidate assignment (atomic, not re-randomized on refresh)
    candidate_x = models.ForeignKey(
        TableExtraction, on_delete=models.PROTECT, null=True, blank=True,
        related_name="review_tasks_as_x",
    )
    candidate_y = models.ForeignKey(
        TableExtraction, on_delete=models.PROTECT, null=True, blank=True,
        related_name="review_tasks_as_y",
    )

    class Meta:
        ordering = ["priority", "created_at"]

    def clean(self):
        super().clean()
        if (
            self.candidate_x_id
            and self.candidate_y_id
            and self.candidate_x_id == self.candidate_y_id
        ):
            raise ValidationError(
                "Candidate X and Candidate Y must differ."
            )
        for extraction in [self.candidate_x, self.candidate_y]:
            if (
                extraction is not None
                and extraction.table_candidate_id
                != self.table_candidate_id
            ):
                raise ValidationError(
                    "Review candidates must belong to the task's table."
                )

    def __str__(self):
        return f"Review: {self.table_candidate}"


class Review(models.Model):
    """A review of two table extraction candidates."""

    PREFERRED_CHOICES = [
        ("candidate_x", "Candidate X"),
        ("candidate_y", "Candidate Y"),
        ("equivalent", "Equivalent"),
        ("neither", "Neither"),
    ]

    CONFIDENCE_CHOICES = [
        ("high", "High"),
        ("medium", "Medium"),
        ("low", "Low"),
    ]

    id = models.AutoField(primary_key=True)
    review_task = models.OneToOneField(ReviewTask, on_delete=models.CASCADE, related_name="review")
    reviewer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="reviews", null=True, blank=True)
    candidate_x = models.ForeignKey(TableExtraction, on_delete=models.PROTECT, related_name="reviews_as_x", null=True, blank=True)
    candidate_y = models.ForeignKey(TableExtraction, on_delete=models.PROTECT, related_name="reviews_as_y", null=True, blank=True)
    candidate_x_score = models.PositiveIntegerField(default=0)
    candidate_y_score = models.PositiveIntegerField(default=0)
    preferred_result = models.CharField(max_length=20, choices=PREFERRED_CHOICES)
    structure_errors = JSONField(default=list, blank=True)
    text_errors = JSONField(default=list, blank=True)
    comment = models.TextField(blank=True)
    confidence = models.CharField(max_length=10, choices=CONFIDENCE_CHOICES, default="medium")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    post_reveal_comment = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Review by {self.reviewer} - {self.review_task.table_candidate}"


class Decision(models.Model):
    """A curator's final decision on a table."""

    id = models.AutoField(primary_key=True)
    table_candidate = models.OneToOneField(TableCandidate, on_delete=models.CASCADE, related_name="decision")
    selected_extraction = models.ForeignKey(TableExtraction, on_delete=models.SET_NULL, null=True, blank=True, related_name="decisions")
    decision = models.CharField(
        max_length=20,
        choices=[
            ("accepted", "Accepted"),
            ("rejected", "Rejected"),
            ("needs_review", "Needs Review"),
            ("manual_correction", "Manual Correction"),
        ],
    )
    decided_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="decisions")
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Decision on {self.table_candidate}"


class AuditEvent(models.Model):
    """Audit trail for significant actions."""

    id = models.AutoField(primary_key=True)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    event_type = models.CharField(max_length=50, db_index=True)
    object_type = models.CharField(max_length=50, blank=True)
    object_id = models.CharField(max_length=100, blank=True)
    before = JSONField(null=True, blank=True)
    after = JSONField(null=True, blank=True)
    request_id = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event_type} - {self.object_type} {self.object_id}"


class ServiceAccount(models.Model):
    """Non-human identity for agents, CI, deployment tools."""

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="service_accounts",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Service account"

    def __str__(self):
        return self.name


class ApiToken(models.Model):
    """Scoped API token for human users or service accounts."""

    # Canonical scope choices (add as needed)
    SCOPE_CHOICES = [
        ("projects:read", "Read projects"),
        ("projects:write", "Create and archive projects"),
        ("documents:read", "Read documents"),
        ("documents:upload", "Upload documents"),
        ("jobs:submit", "Submit processing jobs"),
        ("documents:manage", "Archive and retry documents"),
        ("jobs:read", "Read job status and results"),
        ("tasks:read", "Read review tasks"),
        ("reviews:write", "Submit reviews"),
        ("statistics:read", "Read statistics"),
        ("chat:read", "Read chat"),
        ("chat:write", "Write chat"),
        ("chat:retry", "Retry chat runs"),
        ("chat:manage", "Rename and archive chat"),
        ("diagnostics:read", "Read diagnostics"),
        ("support:read", "Read support bundles"),
        ("support:export", "Export support bundles"),
    ]

    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, null=True, blank=True,
        related_name="api_tokens",
    )
    service_account = models.ForeignKey(
        ServiceAccount, on_delete=models.CASCADE, null=True, blank=True,
        related_name="api_tokens",
    )
    name = models.CharField(max_length=100)
    token_prefix = models.CharField(max_length=8)
    token_hash = models.CharField(max_length=128, db_index=True)
    scopes = JSONField(default=list)
    project = models.ForeignKey(
        "Collection", on_delete=models.CASCADE, null=True, blank=True,
        related_name="api_tokens",
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "API token"

    def __str__(self):
        return f"{self.name} ({self.token_prefix}...)"

    def clean(self):
        if not self.user_id and not self.service_account_id:
            raise ValidationError("Exactly one of user or service_account must be set.")
        if self.user_id and self.service_account_id:
            raise ValidationError("Only one of user or service_account may be set.")

    @staticmethod
    def generate_token():
        """Generate a random 48-char token. Returns raw string."""
        import secrets
        raw = secrets.token_urlsafe(36)  # ~48 chars
        return raw

    @classmethod
    def hash_token(cls, raw_token):
        """Hash a raw token for storage."""
        import hashlib
        return hashlib.sha256(raw_token.encode()).hexdigest()

    @classmethod
    def verify_token(cls, raw_token):
        """Look up a token by its hash. Returns the token or None."""
        hashed = cls.hash_token(raw_token)
        try:
            return cls.objects.get(token_hash=hashed)
        except cls.DoesNotExist:
            return None

    def is_valid(self):
        """Check if this token is usable (not expired, not revoked, owner active)."""
        from django.utils import timezone
        if self.revoked_at:
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        if self.user_id:
            try:
                return self.user.is_active
            except User.DoesNotExist:
                return False
        if self.service_account_id:
            try:
                return self.service_account.is_active
            except ServiceAccount.DoesNotExist:
                return False
        return True

    def touch(self):
        """Update last_used_at (call sparingly to avoid excessive writes)."""
        from django.utils import timezone
        self.last_used_at = timezone.now()
        self.save(update_fields=["last_used_at"])


# ---------------------------------------------------------------------------
# Project membership & authorization
# ---------------------------------------------------------------------------

class ProjectMembership(models.Model):
    """Membership of a user in a project."""

    ROLE_CHOICES = [
        ("owner", "Owner"),
        ("editor", "Editor"),
        ("reviewer", "Reviewer"),
        ("viewer", "Viewer"),
    ]

    id = models.AutoField(primary_key=True)
    project = models.ForeignKey(
        "Collection", on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name="project_memberships",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="viewer")
    invited_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="invited_memberships",
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["project", "user"]
        ordering = ["-joined_at"]

    def __str__(self):
        return f"{self.user.get_username()} @ {self.project.name} ({self.role})"

    @property
    def is_owner(self):
        return self.role == "owner"

    @property
    def can_edit(self):
        return self.role in ("owner", "editor")

    @property
    def can_review(self):
        return self.role in ("owner", "editor", "reviewer")


# ---------------------------------------------------------------------------
# Generic document ingestion layer
# ---------------------------------------------------------------------------

class SourceDocument(models.Model):
    """A source document (uploaded, benchmark, API, network share)."""

    SOURCE_TYPES = [
        ("upload", "User upload"),
        ("benchmark", "Benchmark dataset"),
        ("api", "API submission"),
        ("network_share", "Network share"),
        ("batch_import", "Batch import"),
    ]

    id = models.AutoField(primary_key=True)
    collection = models.ForeignKey(
        Collection, on_delete=models.CASCADE, null=True, blank=True,
        related_name="source_documents",
    )
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPES, default="upload")
    filename = models.CharField(max_length=500)
    file_path = models.CharField(max_length=1000, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    page_count = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="source_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_archived = models.BooleanField(default=False)
    active_document = models.ForeignKey(
        "Document",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="active_for_sources",
        help_text="Currently selected completed processing revision.",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.filename} ({self.get_source_type_display()})"


class ProcessingPreset(models.Model):
    """A named processing configuration presented to users."""

    id = models.AutoField(primary_key=True)
    slug = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    description_short = models.CharField(max_length=200, blank=True)
    profile_a_enabled = models.BooleanField(default=True)
    profile_b_enabled = models.BooleanField(default=False)
    generate_crops = models.BooleanField(default=False)
    create_review_tasks = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return f"{self.name} ({self.slug})"


class ProcessingJob(models.Model):
    """A document processing job submitted by a user or worker."""

    JOB_STATES = [
        ("queued", _("Queued")),
        ("submitting", _("Submitting to processor")),
        ("processing", _("Processing")),
        ("importing", _("Importing results")),
        ("submission_uncertain", _("Submission uncertain")),
        ("interrupted", _("Interrupted")),
        ("completed", _("Completed")),
        ("partial", _("Partially completed")),
        ("failed", _("Failed")),
        ("cancelled", _("Cancelled")),
    ]

    id = models.AutoField(primary_key=True)
    source_document = models.ForeignKey(
        SourceDocument, on_delete=models.CASCADE,
        related_name="processing_jobs",
    )
    preset = models.ForeignKey(
        ProcessingPreset, on_delete=models.PROTECT,
        related_name="processing_jobs",
    )
    # Snapshot of preset config at job creation time
    preset_snapshot = JSONField(default=dict, blank=True)
    state = models.CharField(max_length=20, choices=JOB_STATES, default="queued")
    external_job_id = models.CharField(max_length=200, blank=True)
    processor = models.CharField(max_length=50, blank=True)
    result_document = models.OneToOneField(
        Document,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="processing_job",
        help_text="Immutable processed revision produced by this job.",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    worker_heartbeat_at = models.DateTimeField(null=True, blank=True)
    remote_poll_attempted_at = models.DateTimeField(null=True, blank=True)
    remote_response_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    worker_id = models.CharField(max_length=200, blank=True)
    status_message = models.CharField(max_length=500, blank=True)
    remote_status = models.CharField(max_length=100, blank=True)
    consecutive_poll_errors = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    pages_processed = models.PositiveIntegerField(default=0)
    tables_found = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="processing_jobs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    # Allowed state transitions
    _TRANSITIONS = {
        "queued": ["submitting", "cancelled"],
        "submitting": ["processing", "submission_uncertain", "interrupted", "failed", "cancelled"],
        "processing": ["importing", "interrupted", "failed", "cancelled"],
        "importing": ["completed", "partial", "interrupted", "failed"],
        # Closing an uncertain submission is an operator decision; it does
        # not claim that the remote processor never received the file.
        "submission_uncertain": ["failed", "cancelled"],
        "interrupted": ["processing", "importing", "failed", "cancelled"],
        "completed": [],
        "partial": [],
        "failed": [],
        "cancelled": [],
    }

    def __str__(self):
        return f"Job {self.id}: {self.source_document.filename} ({self.state})"

    def transition_to(self, new_state):
        """Transition to a new state, validating the transition is allowed."""
        allowed = self._TRANSITIONS.get(self.state, [])
        if new_state not in allowed:
            raise ValidationError(
                f"Cannot transition from '{self.state}' to '{new_state}'. "
                f"Allowed: {allowed}"
            )
        old_state = self.state
        self.state = new_state
        from django.utils import timezone
        if new_state in (
            "submission_uncertain", "interrupted", "completed", "partial",
            "failed", "cancelled",
        ):
            self.finished_at = timezone.now()
        elif new_state in ("submitting", "processing", "importing"):
            # A resumed job is active again; its previous terminal timestamp
            # must not make the status page imply that it has finished.
            self.finished_at = None
        if new_state == "submitting":
            self.started_at = timezone.now()
        self.save(update_fields=["state", "started_at", "finished_at"])
        return old_state

    @property
    def is_active(self):
        return self.state in {"submitting", "processing", "importing"}

    def is_stale(self, now=None):
        """Return whether an active job has stopped receiving worker heartbeats."""
        if not self.is_active:
            return False
        from datetime import timedelta

        from django.utils import timezone
        now = now or timezone.now()
        threshold = getattr(settings, "DSW_PROCESSING_STALE_AFTER_SECONDS", 90)
        last_signal = self.worker_heartbeat_at or self.started_at or self.created_at
        return last_signal < now - timedelta(seconds=threshold)

    def capture_preset_snapshot(self):
        """Capture a snapshot of the current preset configuration."""
        self.preset_snapshot = {
            "slug": self.preset.slug,
            "name": self.preset.name,
            "profile_a_enabled": self.preset.profile_a_enabled,
            "profile_b_enabled": self.preset.profile_b_enabled,
            "generate_crops": self.preset.generate_crops,
            "create_review_tasks": self.preset.create_review_tasks,
        }
        self.save(update_fields=["preset_snapshot"])


class ProcessingArtifact(models.Model):
    """A file or data artifact produced by a processing job."""

    ARTIFACT_TYPES = [
        ("page_image", "Page image"),
        ("table_crop", "Table crop image"),
        ("table_extraction", "Table extraction result"),
        ("page_text", "Page text"),
        ("ocr_page", "Visual OCR page result"),
        ("layout_json", "Layout detection JSON"),
        ("otsl", "OTSL table markup"),
    ]

    id = models.AutoField(primary_key=True)
    job = models.ForeignKey(
        ProcessingJob, on_delete=models.CASCADE,
        related_name="artifacts",
    )
    artifact_type = models.CharField(max_length=30, choices=ARTIFACT_TYPES)
    page_number = models.PositiveIntegerField(null=True, blank=True)
    region_id = models.CharField(max_length=100, blank=True)
    file_path = models.CharField(max_length=1000, blank=True)
    data = JSONField(default=dict, blank=True)
    metadata = JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["job", "page_number", "region_id"]

    def clean(self):
        super().clean()
        # File paths must be application-controlled relative identifiers,
        # not arbitrary paths from HTTP/API input
        if self.file_path:
            import os
            # Normalize and reject absolute paths or path traversal
            normalized = os.path.normpath(self.file_path)
            if normalized.startswith("/") or ".." in normalized.split(os.sep):
                raise ValidationError({
                    "file_path": "Artifact paths must be relative, application-controlled identifiers, not absolute paths or paths containing '..'."
                })

    def __str__(self):
        return f"{self.get_artifact_type_display()} - Job {self.job_id}"


class PageRegion(models.Model):
    """A detected region within a document page (generic, not table-specific)."""

    REGION_TYPES = [
        ("text", _("Text block")),
        ("title", _("Title / heading")),
        ("table", _("Table")),
        ("figure", _("Figure / image")),
        ("form", _("Form")),
        ("list", _("List")),
        ("header", _("Page header")),
        ("footer", _("Page footer")),
        ("other", _("Other")),
    ]

    id = models.AutoField(primary_key=True)
    source_document = models.ForeignKey(
        SourceDocument, on_delete=models.CASCADE,
        related_name="regions",
    )
    job = models.ForeignKey(
        ProcessingJob, on_delete=models.CASCADE,
        related_name="regions",
    )
    page = models.ForeignKey(
        Page, on_delete=models.CASCADE, null=True, blank=True,
        related_name="regions",
    )
    page_number = models.PositiveIntegerField()
    region_type = models.CharField(max_length=20, choices=REGION_TYPES)
    # Normalized coordinates: top-left origin, page-relative
    left = models.FloatField()
    top = models.FloatField()
    right = models.FloatField()
    bottom = models.FloatField()
    page_width = models.FloatField(null=True, blank=True)
    page_height = models.FloatField(null=True, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    text = models.TextField(blank=True, default="", help_text="Region text content.")
    metadata = JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["source_document", "page_number", "top", "left"]

    def clean(self):
        super().clean()
        if self.page_id:
            if self.page.page_number != self.page_number:
                raise ValidationError({"page": "Region page number does not match its page."})
            if self.job.result_document_id and self.page.document_id != self.job.result_document_id:
                raise ValidationError({"page": "Region page must belong to the job result revision."})
        # Validate normalized coordinates: 0 <= left < right <= 1, 0 <= top < bottom <= 1
        if self.left is not None and self.right is not None:
            if not (0 <= self.left < self.right <= 1):
                raise ValidationError({
                    "left": "left must be >= 0 and < right",
                    "right": "right must be > left and <= 1",
                })
        if self.top is not None and self.bottom is not None:
            if not (0 <= self.top < self.bottom <= 1):
                raise ValidationError({
                    "top": "top must be >= 0 and < bottom",
                    "bottom": "bottom must be > top and <= 1",
                })

    def __str__(self):
        return f"{self.get_region_type_display()} - Page {self.page_number}"

    @property
    def active_correction(self):
        return self.corrections.filter(status="active").order_by("-created_at", "-id").first()

    @property
    def effective_text(self):
        correction = self.corrections.filter(operation="text", status="active").order_by("-created_at", "-id").first()
        if correction:
            return correction.after.get("text", self.text)
        return self.text

    @property
    def effective_region_type(self):
        correction = self.corrections.filter(operation="type", status="active").order_by("-created_at", "-id").first()
        if correction:
            return correction.after.get("region_type", self.region_type)
        return self.region_type

    @property
    def is_suppressed(self):
        return self.corrections.filter(operation="suppress", status="active").exists()


class SearchPassage(models.Model):
    """Revision-scoped searchable text with stable scan provenance."""

    PASSAGE_TYPES = [
        ("heading", "Heading"),
        ("paragraph", "Paragraph"),
        ("region", "Region"),
        ("table", "Table"),
        ("table_row", "Table row"),
        ("metadata", "Metadata"),
    ]

    project = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name="search_passages")
    source_document = models.ForeignKey(SourceDocument, on_delete=models.CASCADE, related_name="search_passages")
    processed_revision = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="search_passages")
    processing_job = models.ForeignKey(ProcessingJob, on_delete=models.CASCADE, related_name="search_passages")
    page = models.ForeignKey(Page, on_delete=models.CASCADE, null=True, blank=True, related_name="search_passages")
    page_region = models.ForeignKey(PageRegion, on_delete=models.CASCADE, null=True, blank=True, related_name="search_passages")
    passage_type = models.CharField(max_length=30, choices=PASSAGE_TYPES, default="region")
    archival_identifier = models.CharField(max_length=255, blank=True)
    heading_context = models.CharField(max_length=500, blank=True)
    text = models.TextField()
    normalized_text = models.TextField(db_index=True)
    ordinal = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["page__page_number", "ordinal", "id"]
        indexes = [
            models.Index(fields=["project", "normalized_text"]),
            models.Index(fields=["source_document", "processed_revision"]),
        ]


class ChatThread(models.Model):
    """A persistent, revision-scoped research conversation."""

    project = models.ForeignKey(Collection, on_delete=models.CASCADE, null=True, blank=True, related_name="chat_threads")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="chat_threads")
    title = models.CharField(max_length=255, blank=True)
    search_enabled = models.BooleanField(default=False)
    scope_mode = models.CharField(max_length=20, choices=[("project", "Project"), ("all", "All accessible projects")], default="project")
    scope_config = JSONField(default=dict, blank=True)
    search_project = models.ForeignKey(Collection, on_delete=models.SET_NULL, null=True, blank=True, related_name="search_chat_threads")
    selected_revisions = JSONField(default=list, blank=True)
    scope_snapshot = JSONField(default=dict, blank=True)
    model = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    selected_sources = models.ManyToManyField(SourceDocument, related_name="chat_threads", blank=True)
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["-updated_at", "-id"]


class ChatMessage(models.Model):
    """One user or assistant message in a persistent thread."""

    ROLES = [("user", "User"), ("assistant", "Assistant"), ("system", "System")]
    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=20, choices=ROLES)
    text = models.TextField(blank=True)
    ordinal = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["ordinal", "id"]
        unique_together = [("thread", "ordinal")]


class ChatRun(models.Model):
    """Durable asynchronous evidence retrieval and model-generation run."""

    STATES = [
        ("queued", "Queued"), ("retrieving", "Retrieving evidence"),
        ("assembling", "Assembling context"), ("generating", "Asking the model"),
        ("validating", "Validating citations"), ("completed", "Completed"),
        ("failed", "Failed"), ("cancelled", "Cancelled"),
    ]
    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name="runs")
    user_message = models.ForeignKey(ChatMessage, on_delete=models.CASCADE, related_name="chat_runs")
    assistant_message = models.OneToOneField(ChatMessage, on_delete=models.SET_NULL, null=True, blank=True, related_name="assistant_run")
    state = models.CharField(max_length=20, choices=STATES, default="queued")
    status_message = models.CharField(max_length=500, blank=True)
    error_message = models.TextField(blank=True)
    error_code = models.CharField(max_length=60, blank=True)
    worker_id = models.CharField(max_length=200, blank=True)
    worker_heartbeat_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    retrieval_query = models.TextField(blank=True)
    scope_snapshot = JSONField(default=dict, blank=True)
    token_budget = models.PositiveIntegerField(default=12000)
    source_tokens = models.PositiveIntegerField(default=0)
    model_metadata = JSONField(default=dict, blank=True)
    request_id = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class ChatRunEvent(models.Model):
    """Append-only, sanitized timeline for reconstructing a chat run."""
    run = models.ForeignKey(ChatRun, on_delete=models.CASCADE, related_name="events")
    name = models.CharField(max_length=40)
    worker_id = models.CharField(max_length=200, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    metadata = JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class WorkerHeartbeat(models.Model):
    """Last liveness signal from a running processing/chat worker."""
    worker_id = models.CharField(max_length=200, unique=True)
    last_seen = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_seen"]


class EvidenceItem(models.Model):
    """Exact evidence selected for a run, retaining retrieval explanation."""

    run = models.ForeignKey(ChatRun, on_delete=models.CASCADE, related_name="evidence_items")
    marker = models.CharField(max_length=20)
    source_document = models.ForeignKey(SourceDocument, on_delete=models.CASCADE)
    processed_revision = models.ForeignKey(Document, on_delete=models.CASCADE)
    processing_job = models.ForeignKey(ProcessingJob, on_delete=models.CASCADE)
    page = models.ForeignKey(Page, on_delete=models.CASCADE, null=True, blank=True)
    page_region = models.ForeignKey(PageRegion, on_delete=models.CASCADE, null=True, blank=True)
    passage = models.ForeignKey(SearchPassage, on_delete=models.SET_NULL, null=True, blank=True)
    text = models.TextField()
    page_text = models.TextField(blank=True)
    retrieval_method = models.CharField(max_length=50, blank=True)
    selection_reason = models.CharField(max_length=255, blank=True)
    score = models.FloatField(null=True, blank=True)
    ordinal = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["ordinal", "id"]
        unique_together = [("run", "marker")]


class RegionCorrection(models.Model):
    """Auditable human correction layered over immutable machine output."""

    OPERATIONS = [
        ("text", "Correct text"),
        ("type", "Change region type"),
        ("suppress", "Suppress region"),
        ("note", "Add curator note"),
    ]
    STATUSES = [("active", "Active"), ("reverted", "Reverted")]

    region = models.ForeignKey(PageRegion, on_delete=models.CASCADE, related_name="corrections")
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="region_corrections")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="region_corrections")
    operation = models.CharField(max_length=20, choices=OPERATIONS)
    before = JSONField(default=dict)
    after = JSONField(default=dict)
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default="active")
    created_at = models.DateTimeField(auto_now_add=True)
    reverted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def clean(self):
        super().clean()
        if self.region_id and self.document_id and self.region.page.document_id != self.document_id:
            raise ValidationError({"document": "Correction revision must match the region page revision."})
        if self.operation == "text" and "text" not in self.after:
            raise ValidationError({"after": "Text corrections must provide after.text."})
        if self.operation == "type" and self.after.get("region_type") not in dict(PageRegion.REGION_TYPES):
            raise ValidationError({"after": "Type corrections must provide a valid region_type."})

    def __str__(self):
        return f"{self.get_operation_display()} on region {self.region_id}"


class OcrRequest(models.Model):
    """An immutable visual-OCR candidate for a page or detected region.

    The request and provider response are retained even when a curator rejects
    the candidate. Acceptance is represented by a normal RegionCorrection so
    existing correction/revert semantics remain intact.
    """

    TARGETS = [("region", "Region"), ("page", "Page")]
    STATES = [
        ("queued", "Queued"), ("processing", "Processing"),
        ("completed", "Completed"), ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    ]

    id = models.AutoField(primary_key=True)
    source_document = models.ForeignKey(SourceDocument, on_delete=models.CASCADE, related_name="ocr_requests")
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="ocr_requests")
    page = models.ForeignKey(Page, on_delete=models.PROTECT, related_name="ocr_requests")
    region = models.ForeignKey(PageRegion, on_delete=models.PROTECT, null=True, blank=True, related_name="ocr_requests")
    target = models.CharField(max_length=20, choices=TARGETS, default="region")
    provider = models.CharField(max_length=80, default="openai-compatible")
    model = models.CharField(max_length=200, blank=True)
    prompt = models.TextField()
    input_sha256 = models.CharField(max_length=64, blank=True)
    input_metadata = JSONField(default=dict, blank=True)
    state = models.CharField(max_length=20, choices=STATES, default="queued")
    candidate_text = models.TextField(blank=True)
    raw_response = JSONField(default=dict, blank=True)
    metadata = JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="ocr_requests")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    accepted_correction = models.OneToOneField("RegionCorrection", on_delete=models.SET_NULL, null=True, blank=True, related_name="ocr_request")

    class Meta:
        ordering = ["-created_at", "-id"]

    def clean(self):
        super().clean()
        if self.region_id and self.region.page_id != self.page_id:
            raise ValidationError({"region": "OCR region must belong to the selected page."})
        if self.document_id and self.page.document_id != self.document_id:
            raise ValidationError({"page": "OCR page must belong to the selected revision."})
        if self.source_document_id and self.document.processing_job.source_document_id != self.source_document_id:
            raise ValidationError({"document": "OCR revision must belong to the selected source document."})

    def __str__(self):
        target = f"region {self.region_id}" if self.region_id else f"page {self.page_id}"
        return f"OCR {self.id}: {target} ({self.state})"
