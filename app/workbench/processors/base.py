"""
Base document processor interface.

Subclasses implement the actual extraction logic (Docling, Granite, etc.).
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProcessorResult:
    """Standardized result from a document processor."""
    pages_processed: int = 0
    tables_found: int = 0
    page_images: dict = field(default_factory=dict)  # page_num -> {data, format}
    page_texts: dict = field(default_factory=dict)  # page_num -> text
    layout_json: Optional[str] = None  # relative path to layout JSON
    regions: list = field(default_factory=list)  # list of region dicts
    table_crops: dict = field(default_factory=dict)  # table_id -> relative_path
    table_extractions: dict = field(default_factory=dict)  # table_id -> extraction dict
    processor_metadata: dict = field(default_factory=dict)
    error_summary: Optional[str] = None


class DocumentProcessor(ABC):
    """Abstract base for document processors."""

    @abstractmethod
    def submit(self, source_document, configuration: dict) -> str:
        """
        Submit a document for processing.

        Returns:
            external_job_id: String identifier for the external job.
        """

    @abstractmethod
    def get_status(self, external_job_id: str) -> dict:
        """
        Get the status of an external job.

        Returns:
            dict with 'state', 'progress', 'error' keys.
        """

    @abstractmethod
    def collect_results(self, external_job_id: str) -> ProcessorResult:
        """
        Collect and parse results from a completed job.

        Returns:
            ProcessorResult with all extracted data.
        """

    @abstractmethod
    def cancel(self, external_job_id: str) -> bool:
        """
        Cancel an in-progress job.

        Returns:
            True if cancellation was successful.
        """
