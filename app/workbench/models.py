"""
Archive Structure Workbench - Core Data Models.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import JSONField
from django.urls import reverse

User = get_user_model()


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
