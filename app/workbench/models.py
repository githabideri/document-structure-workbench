"""
Document Structure Workbench - Core Data Models.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
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
        unique_together = ["collection", "sha256"]
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
        ("documents:read", "Read documents"),
        ("documents:upload", "Upload documents"),
        ("jobs:submit", "Submit processing jobs"),
        ("jobs:read", "Read job status and results"),
        ("tasks:read", "Read review tasks"),
        ("reviews:write", "Submit reviews"),
        ("statistics:read", "Read statistics"),
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
        """Generate a random 48-char token and return (raw, hash)."""
        import secrets
        from django.utils.crypto import get_random_string
        raw = secrets.token_urlsafe(36)  # ~48 chars
        return raw, raw  # hash later in save()

    @classmethod
    def verify_token(cls, raw_token):
        """Look up a token by its hash. Returns the token or None."""
        try:
            return cls.objects.get(token_hash=raw_token)
        except cls.DoesNotExist:
            return None


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
        ("queued", "Queued"),
        ("submitting", "Submitting to processor"),
        ("processing", "Processing"),
        ("importing", "Importing results"),
        ("completed", "Completed"),
        ("partial", "Partially completed"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
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
    state = models.CharField(max_length=20, choices=JOB_STATES, default="queued")
    external_job_id = models.CharField(max_length=200, blank=True)
    processor = models.CharField(max_length=50, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
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

    def __str__(self):
        return f"Job {self.id}: {self.source_document.filename} ({self.state})"


class ProcessingArtifact(models.Model):
    """A file or data artifact produced by a processing job."""

    ARTIFACT_TYPES = [
        ("page_image", "Page image"),
        ("table_crop", "Table crop image"),
        ("table_extraction", "Table extraction result"),
        ("page_text", "Page text"),
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

    def __str__(self):
        return f"{self.get_artifact_type_display()} - Job {self.job_id}"


class PageRegion(models.Model):
    """A detected region within a document page (generic, not table-specific)."""

    REGION_TYPES = [
        ("text", "Text block"),
        ("title", "Title / heading"),
        ("table", "Table"),
        ("figure", "Figure / image"),
        ("form", "Form"),
        ("list", "List"),
        ("header", "Page header"),
        ("footer", "Page footer"),
        ("other", "Other"),
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
    metadata = JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["source_document", "page_number", "top", "left"]

    def __str__(self):
        return f"{self.get_region_type_display()} - Page {self.page_number}"
