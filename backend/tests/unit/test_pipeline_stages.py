"""Resumable pipeline mechanics.

The requirement under test: a run over 500 PDFs that fails on document 120 must
not restart from zero, and one document's failure must not stop the run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.exceptions import DocumentProcessingError
from app.db.enums import ProcessingStage
from app.ingestion.pipeline import (
    DocumentOutcome,
    DocumentPipeline,
    PipelineConfig,
    StageResult,
    discover_pdfs,
    hash_file,
    resume_stage_for,
)


class TestStageOrdering:
    def test_stages_are_ordered(self) -> None:
        assert ProcessingStage.EXTRACTED.order < ProcessingStage.CHUNKED.order
        assert ProcessingStage.CHUNKED.order < ProcessingStage.INDEXED.order

    def test_is_at_least_compares_progress(self) -> None:
        assert ProcessingStage.CHUNKED.is_at_least(ProcessingStage.EXTRACTED)
        assert not ProcessingStage.EXTRACTED.is_at_least(ProcessingStage.CHUNKED)

    def test_a_stage_has_reached_itself(self) -> None:
        assert ProcessingStage.CHUNKED.is_at_least(ProcessingStage.CHUNKED)

    def test_failed_has_reached_nothing(self) -> None:
        # Partial output from a stage that raised cannot be trusted.
        assert not ProcessingStage.FAILED.is_at_least(ProcessingStage.UPLOADED)
        assert not ProcessingStage.FAILED.is_at_least(ProcessingStage.EXTRACTED)

    def test_terminal_stages(self) -> None:
        assert ProcessingStage.INDEXED.is_terminal
        assert ProcessingStage.FAILED.is_terminal
        assert not ProcessingStage.CHUNKED.is_terminal

    def test_every_stage_has_an_order(self) -> None:
        for stage in ProcessingStage:
            assert isinstance(stage.order, int)


class TestResumePoint:
    def test_failed_document_restarts_from_the_beginning(self) -> None:
        assert resume_stage_for(ProcessingStage.FAILED) is ProcessingStage.UPLOADED

    def test_unknown_document_starts_from_the_beginning(self) -> None:
        assert resume_stage_for(None) is ProcessingStage.UPLOADED

    def test_partial_document_resumes_where_it_stopped(self) -> None:
        assert resume_stage_for(ProcessingStage.TABLES_EXTRACTED) is (
            ProcessingStage.TABLES_EXTRACTED
        )


class TestResumeSkipsCompletedWork:
    def test_stages_up_to_the_resume_point_are_not_re_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pdf = tmp_path / "bylaw.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")

        pipeline = DocumentPipeline(PipelineConfig(ocr_enabled=False))
        called: list[str] = []

        for name in (
            "_stage_extract",
            "_stage_ocr",
            "_stage_tables",
            "_stage_metadata",
            "_stage_sections",
            "_stage_chunk",
        ):
            monkeypatch.setattr(
                pipeline,
                name,
                lambda _outcome, _name=name: called.append(_name),
            )

        pipeline.process(pdf, resume_from=ProcessingStage.METADATA_DETECTED)

        # Extraction, OCR, tables and metadata already succeeded previously.
        assert called == ["_stage_sections", "_stage_chunk"]

    def test_a_full_run_executes_every_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pdf = tmp_path / "bylaw.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")

        pipeline = DocumentPipeline(PipelineConfig(ocr_enabled=False))
        called: list[str] = []
        for name in (
            "_stage_extract",
            "_stage_ocr",
            "_stage_tables",
            "_stage_metadata",
            "_stage_sections",
            "_stage_chunk",
        ):
            monkeypatch.setattr(pipeline, name, lambda _outcome, _name=name: called.append(_name))

        outcome = pipeline.process(pdf)
        assert len(called) == 6
        assert outcome.stage is ProcessingStage.CHUNKED
        assert outcome.succeeded


class TestFailureIsolation:
    def test_a_failing_stage_stops_that_document_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pdf = tmp_path / "bylaw.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")

        pipeline = DocumentPipeline(PipelineConfig(ocr_enabled=False))

        def boom(_outcome: DocumentOutcome) -> None:
            raise DocumentProcessingError("extraction blew up", stage="extracted")

        monkeypatch.setattr(pipeline, "_stage_extract", boom)

        # No exception escapes: the run must continue to document 121.
        outcome = pipeline.process(pdf)

        assert not outcome.succeeded
        assert outcome.stage is ProcessingStage.FAILED
        assert outcome.failed_stage is ProcessingStage.EXTRACTED
        assert outcome.error is not None
        assert outcome.error.code == "document_processing_error"

    def test_unexpected_exceptions_are_wrapped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pdf = tmp_path / "bylaw.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")

        pipeline = DocumentPipeline(PipelineConfig(ocr_enabled=False))
        monkeypatch.setattr(
            pipeline,
            "_stage_extract",
            lambda _outcome: (_ for _ in ()).throw(ValueError("unexpected")),
        )

        outcome = pipeline.process(pdf)
        assert isinstance(outcome.error, DocumentProcessingError)
        assert outcome.error.stage == "extracted"

    def test_later_stages_do_not_run_after_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pdf = tmp_path / "bylaw.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")

        pipeline = DocumentPipeline(PipelineConfig(ocr_enabled=False))
        ran: list[str] = []

        monkeypatch.setattr(
            pipeline,
            "_stage_extract",
            lambda _outcome: (_ for _ in ()).throw(
                DocumentProcessingError("nope", stage="extracted")
            ),
        )
        monkeypatch.setattr(pipeline, "_stage_chunk", lambda _outcome: ran.append("chunk"))

        pipeline.process(pdf)
        assert ran == []


class TestStageRecording:
    def test_every_stage_is_reported_to_the_recorder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pdf = tmp_path / "bylaw.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")

        recorded: list[tuple[str, StageResult]] = []
        pipeline = DocumentPipeline(
            PipelineConfig(ocr_enabled=False),
            recorder=lambda sha, result: recorded.append((sha, result)),
        )
        for name in (
            "_stage_extract",
            "_stage_ocr",
            "_stage_tables",
            "_stage_metadata",
            "_stage_sections",
            "_stage_chunk",
        ):
            monkeypatch.setattr(pipeline, name, lambda _outcome: None)

        pipeline.process(pdf)

        assert len(recorded) == 6
        assert all(result.succeeded for _, result in recorded)
        assert all(result.duration_ms >= 0 for _, result in recorded)

    def test_failures_are_recorded_with_the_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pdf = tmp_path / "bylaw.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")

        recorded: list[StageResult] = []
        pipeline = DocumentPipeline(
            PipelineConfig(ocr_enabled=False),
            recorder=lambda _sha, result: recorded.append(result),
        )
        monkeypatch.setattr(
            pipeline,
            "_stage_extract",
            lambda _outcome: (_ for _ in ()).throw(
                DocumentProcessingError("bad pdf", stage="extracted")
            ),
        )

        pipeline.process(pdf)

        assert len(recorded) == 1
        assert not recorded[0].succeeded
        assert recorded[0].error_message == "bad pdf"


class TestContentHashing:
    def test_identical_content_hashes_identically(self, tmp_path: Path) -> None:
        first = tmp_path / "a.pdf"
        second = tmp_path / "b.pdf"
        first.write_bytes(b"same bytes")
        second.write_bytes(b"same bytes")
        assert hash_file(first) == hash_file(second)

    def test_different_content_hashes_differently(self, tmp_path: Path) -> None:
        first = tmp_path / "a.pdf"
        second = tmp_path / "b.pdf"
        first.write_bytes(b"one")
        second.write_bytes(b"two")
        assert hash_file(first) != hash_file(second)

    def test_hash_is_sha256_length(self, tmp_path: Path) -> None:
        path = tmp_path / "a.pdf"
        path.write_bytes(b"content")
        assert len(hash_file(path)) == 64


class TestDiscovery:
    def test_finds_pdfs_recursively(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.pdf").write_bytes(b"x")
        (tmp_path / "sub" / "b.pdf").write_bytes(b"x")
        (tmp_path / "notes.txt").write_text("ignore me")

        found = [path.name for path in discover_pdfs(tmp_path)]
        assert found == ["a.pdf", "b.pdf"]

    def test_order_is_stable(self, tmp_path: Path) -> None:
        # "It stopped at document 120" must be reproducible.
        for name in ("c.pdf", "a.pdf", "b.pdf"):
            (tmp_path / name).write_bytes(b"x")
        assert [p.name for p in discover_pdfs(tmp_path)] == ["a.pdf", "b.pdf", "c.pdf"]

    def test_hidden_files_are_skipped(self, tmp_path: Path) -> None:
        (tmp_path / ".hidden.pdf").write_bytes(b"x")
        (tmp_path / "real.pdf").write_bytes(b"x")
        assert [p.name for p in discover_pdfs(tmp_path)] == ["real.pdf"]

    def test_oversized_files_are_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "big.pdf").write_bytes(b"x" * 5000)
        (tmp_path / "small.pdf").write_bytes(b"x" * 10)
        found = [p.name for p in discover_pdfs(tmp_path, max_size_bytes=1000)]
        assert found == ["small.pdf"]

    def test_case_insensitive_extension(self, tmp_path: Path) -> None:
        (tmp_path / "SCAN.PDF").write_bytes(b"x")
        assert [p.name for p in discover_pdfs(tmp_path)] == ["SCAN.PDF"]

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(DocumentProcessingError, match="does not exist"):
            list(discover_pdfs(tmp_path / "nope"))

    def test_empty_directory(self, tmp_path: Path) -> None:
        assert list(discover_pdfs(tmp_path)) == []
