"""
tests/test_research_export_service.py
-------------------------------------
Unit tests for ResearchExportService covering:
- CSV export format, column headers, source attribution, formula injection sanitization.
- XLSX multi-sheet generation, formula injection protection, missing values.
- PDF report generation, table layout, conflict flagging, and text extraction verification.
"""

import io
import csv
import pytest
from openpyxl import load_workbook
from pypdf import PdfReader

from models.schemas import (
    ComparisonResult,
    NormalizedRecord,
    SourceStatus,
    ExtractedFieldEvidence,
    ComparisonConflict,
)
from services.research_export_service import (
    ResearchExportService,
    sanitize_spreadsheet_value,
)


@pytest.fixture
def sample_comparison_result():
    src1 = "https://store-a.example.com/item/laptop"
    src2 = "https://store-b.example.com/product/laptop"
    src3 = "https://store-c.example.com/unreachable"

    sources = [
        SourceStatus(url=src1, status="completed", title="Store A Laptop", extracted_fields_count=3, duration_ms=450.0),
        SourceStatus(url=src2, status="completed", title="Store B Laptop", extracted_fields_count=3, duration_ms=520.0),
        SourceStatus(url=src3, status="failed", error="404 Not Found", duration_ms=150.0),
    ]

    conflict = ComparisonConflict(
        field="price",
        values={src1: "$999.00", src2: "$1,049.00"},
        description="Conflicting 'price' values: '$999.00' (store-a) vs '$1,049.00' (store-b)",
    )

    rec1 = NormalizedRecord(
        record_id="rec_001",
        name="UltraBook Pro 14",
        canonical_key="ultrabook pro 14",
        source_urls=[src1, src2],
        is_duplicate_across_sources=True,
        fields={
            "price": "$999.00",
            "ram": "16 GB",
            "storage": "512 GB",
            "warranty": "N/A",  # missing criterion
        },
        evidence={
            "price": ExtractedFieldEvidence(value="$999.00", source_url=src1, extracted_at="2026-10-25T10:00:00", field_name="price", confidence=1.0, evidence_text="$999 on sale"),
            "ram": ExtractedFieldEvidence(value="16 GB", source_url=src1, extracted_at="2026-10-25T10:00:00", field_name="ram", confidence=0.9),
            "storage": ExtractedFieldEvidence(value="512 GB", source_url=src2, extracted_at="2026-10-25T10:00:00", field_name="storage", confidence=0.95),
            "warranty": ExtractedFieldEvidence(value="N/A", source_url=src1, extracted_at="2026-10-25T10:00:00", field_name="warranty", status="missing", confidence=0.0),
        },
        conflicts=[conflict],
    )

    # Record with formula injection attempts
    rec2 = NormalizedRecord(
        record_id="rec_002",
        name="=cmd|' /C calc'!A0",  # formula injection in name
        canonical_key="injection item",
        source_urls=[src1],
        is_duplicate_across_sources=False,
        fields={
            "price": "@SUM(1+1)",      # formula injection in price
            "ram": "+100GB",           # formula injection in ram
            "storage": "-256GB",       # formula injection in storage
            "warranty": "\t=2years",   # formula injection with tab prefix
        },
        evidence={},
        conflicts=[],
    )

    return ComparisonResult(
        goal="Compare laptop prices and specs across online stores",
        criteria=["price", "ram", "storage", "warranty"],
        sources=sources,
        records=[rec1, rec2],
        summary={
            "total_sources": 3,
            "successful_sources": 2,
            "failed_sources": 1,
            "total_records": 2,
        },
    )


def test_sanitize_spreadsheet_value_formula_injection():
    """Verify formula injection prefixes are properly escaped."""
    assert sanitize_spreadsheet_value("=cmd|' /C calc'!A0") == "'=cmd|' /C calc'!A0"
    assert sanitize_spreadsheet_value("+12345") == "'+12345"
    assert sanitize_spreadsheet_value("-250") == "'-250"
    assert sanitize_spreadsheet_value("@SUM(A1:A10)") == "'@SUM(A1:A10)"
    assert sanitize_spreadsheet_value("\t=danger") == "'\t=danger"
    assert sanitize_spreadsheet_value("\r=danger") == "'\r=danger"
    assert sanitize_spreadsheet_value("Normal Text") == "Normal Text"
    assert sanitize_spreadsheet_value(12345) == 12345
    assert sanitize_spreadsheet_value(None) == "N/A"


def test_export_csv(sample_comparison_result):
    """Verify CSV export columns, contents, and formula injection sanitization."""
    csv_bytes = ResearchExportService.export_csv(sample_comparison_result)
    assert csv_bytes is not None
    assert len(csv_bytes) > 0

    csv_text = csv_bytes.decode("utf-8")
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)

    # Check header
    header = rows[0]
    assert header == ["Record ID", "Entity Name", "price", "ram", "storage", "warranty", "Source URLs", "Duplicate Across Sources", "Conflicts Flagged"]

    # Check row 1 (UltraBook Pro 14)
    r1 = rows[1]
    assert r1[0] == "rec_001"
    assert r1[1] == "UltraBook Pro 14"
    assert r1[2] == "$999.00"
    assert r1[3] == "16 GB"
    assert r1[4] == "512 GB"
    assert r1[5] == "N/A"
    assert "store-a.example.com" in r1[6]
    assert "store-b.example.com" in r1[6]
    assert r1[7] == "Yes"
    assert "Conflicting 'price' values" in r1[8]

    # Check row 2 (Formula Injection record)
    r2 = rows[2]
    assert r2[1].startswith("'=")   # sanitized
    assert r2[2].startswith("'@")   # sanitized
    assert r2[3].startswith("'+")   # sanitized
    assert r2[4].startswith("'-")   # sanitized


def test_export_xlsx(sample_comparison_result):
    """Verify XLSX multi-sheet generation, structure, and sanitization."""
    xlsx_bytes = ResearchExportService.export_xlsx(sample_comparison_result)
    assert xlsx_bytes is not None
    assert len(xlsx_bytes) > 0

    wb = load_workbook(io.BytesIO(xlsx_bytes))
    sheet_names = wb.sheetnames
    assert "Comparison Summary" in sheet_names
    assert "Source Evidence" in sheet_names
    assert "Sources Audited" in sheet_names

    # Check Sheet 1 (Comparison Summary)
    ws1 = wb["Comparison Summary"]
    found_rec1 = False
    found_rec2 = False
    for row in ws1.iter_rows(values_only=True):
        if row and row[0] == "rec_001":
            found_rec1 = True
            assert row[1] == "UltraBook Pro 14"
            assert row[2] == "$999.00"
        elif row and row[0] == "rec_002":
            found_rec2 = True
            assert str(row[1]).startswith("'=")
    assert found_rec1
    assert found_rec2

    # Check Sheet 3 (Sources Audited)
    ws3 = wb["Sources Audited"]
    sources_in_sheet = [row[0] for row in ws3.iter_rows(values_only=True) if row]
    assert "https://store-a.example.com/item/laptop" in sources_in_sheet
    assert "https://store-c.example.com/unreachable" in sources_in_sheet


def test_export_pdf(sample_comparison_result):
    """Verify PDF export produces valid, readable PDF with comparison table and metadata."""
    pdf_bytes = ResearchExportService.export_pdf(sample_comparison_result)
    assert pdf_bytes is not None
    assert len(pdf_bytes) > 100
    assert pdf_bytes.startswith(b"%PDF-")

    # Parse with PdfReader to confirm PDF structure and readable text
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) >= 1

    first_page_text = reader.pages[0].extract_text()
    assert "MULTI-WEBSITE RESEARCH REPORT" in first_page_text
    assert "UltraBook Pro 14" in first_page_text
    assert "store-a.example.com" in first_page_text
    assert "Flagged Data Inconsistencies" in first_page_text or len(reader.pages) > 1
