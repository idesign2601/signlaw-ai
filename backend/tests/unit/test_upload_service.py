"""Upload handling.

The parts worth pinning are the ones where a mistake is silent: a filename that
strips the municipality out, and a path that escapes the corpus directory.
"""

from __future__ import annotations

import pytest

from app.schemas.admin import DocumentState
from app.services.upload_service import UploadRequest, build_filename, store_upload

PDF = b"%PDF-1.7\n%fake\n"


def _request(**overrides: object) -> UploadRequest:
    base = {
        "municipality_slug": "burnaby",
        "title": "Sign Bylaw",
        "year": 1972,
        "original_filename": "scan001.pdf",
        "content": PDF,
    }
    base.update(overrides)
    return UploadRequest(**base)  # type: ignore[arg-type]


class TestFilename:
    def test_carries_municipality_title_and_year(self) -> None:
        """The detector reads only the basename.

        An uploaded ``scan001.pdf`` gives it nothing, and it falls back to
        whatever the cover page says — which for an amending bylaw is often the
        wrong municipality entirely.
        """
        assert build_filename(_request()) == "burnaby_sign-bylaw_1972.pdf"

    def test_year_is_optional(self) -> None:
        assert build_filename(_request(year=None)) == "burnaby_sign-bylaw.pdf"

    def test_punctuation_is_flattened(self) -> None:
        name = build_filename(_request(title="Sign By-law (Consolidated) #11879"))
        assert name == "burnaby_sign-by-law-consolidated-11879_1972.pdf"

    def test_separators_cannot_survive_the_title(self) -> None:
        """A title is operator input and must not become a path."""
        name = build_filename(_request(title="../../etc/passwd"))
        assert "/" not in name
        assert ".." not in name


class TestStoreUpload:
    def test_writes_into_the_corpus_directory(self, tmp_path, settings_factory) -> None:
        settings = settings_factory(ingestion={"corpus_dir": tmp_path / "corpus"})
        path = store_upload(_request(), settings)

        assert path.parent == (tmp_path / "corpus").resolve()
        assert path.read_bytes() == PDF

    def test_non_pdf_bytes_are_rejected(self, tmp_path, settings_factory) -> None:
        """Checked against the bytes, not the declared content type.

        The client controls the content type and can simply be wrong; a
        mislabelled file would otherwise fail much later, inside extraction,
        with a far less useful message.
        """
        settings = settings_factory(ingestion={"corpus_dir": tmp_path / "corpus"})

        with pytest.raises(ValueError, match="not a PDF"):
            store_upload(_request(content=b"GIF89a"), settings)

    def test_traversal_in_the_title_cannot_escape(self, tmp_path, settings_factory) -> None:
        settings = settings_factory(ingestion={"corpus_dir": tmp_path / "corpus"})
        path = store_upload(_request(title="../../../etc/passwd"), settings)

        assert path.is_relative_to((tmp_path / "corpus").resolve())


class TestDocumentState:
    @pytest.mark.parametrize(
        ("stage", "expected"),
        [
            ("uploaded", DocumentState.UPLOADED),
            ("extracted", DocumentState.PROCESSING),
            ("chunked", DocumentState.PROCESSING),
            ("embedded", DocumentState.PROCESSING),
            ("indexed", DocumentState.COMPLETED),
            ("failed", DocumentState.FAILED),
            (None, DocumentState.UPLOADED),
        ],
    )
    def test_pipeline_stages_collapse(self, stage: str | None, expected: DocumentState) -> None:
        assert DocumentState.from_stage(stage) is expected

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("pending", DocumentState.UPLOADED),
            ("running", DocumentState.PROCESSING),
            ("completed", DocumentState.COMPLETED),
            ("failed", DocumentState.FAILED),
            ("cancelled", DocumentState.FAILED),
            ("completed_with_errors", DocumentState.FAILED),
        ],
    )
    def test_job_statuses_collapse(self, status: str, expected: DocumentState) -> None:
        assert DocumentState.from_job(status) is expected

    def test_partial_success_is_not_reported_as_success(self) -> None:
        """completed_with_errors means something did not index.

        Showing it as completed would hide a document the corpus is missing,
        and the gap only surfaces later as an answer that should have existed.
        """
        assert DocumentState.from_job("completed_with_errors") is DocumentState.FAILED
