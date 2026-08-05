"""Ingestion: PDFs in, citable chunks out.

Stage order, tracked per document so a long run is resumable:

    uploaded -> extracted -> ocr_completed -> tables_extracted
             -> metadata_detected -> sections_parsed -> chunked
             -> embedded -> indexed

Everything through ``chunked`` lives here. Embedding and indexing arrive in
Phase 3.
"""
