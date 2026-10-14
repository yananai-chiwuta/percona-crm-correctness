"""Synthetic local CRM correctness experiment; no external API effects."""

from .processor import Event, ProcessingResult, process_event

__all__ = ["Event", "ProcessingResult", "process_event"]
