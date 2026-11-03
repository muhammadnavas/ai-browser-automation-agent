from types import SimpleNamespace

try:
    from pypdf import PdfReader
except ImportError:
    from PyPDF2 import PdfReader

from backend.services.report_generation_service import ReportGenerator


def test_report_generates_readable_pdf_with_failed_status(tmp_path):
    task = SimpleNamespace(
        status="FAILED",
        goal="Find three AI news headlines",
        steps=[SimpleNamespace(
            action="TYPE_TEXT",
            step_number=1,
            description="Type the search query",
            timestamp=1791928447,
            duration_ms=1200,
            url="https://www.google.com/search?q=private-query",
            reasoning="The search field was located.",
            extracted_data=None,
            error="Gemini quota exhausted (429 RESOURCE_EXHAUSTED)",
            screenshot_path=None,
            screenshot_url=None,
            approval_required=False,
            human_intervention_needed=False,
        )],
        created_at=1791928394,
        updated_at=1791928487,
        error="Outcome verification failed because the model quota was exhausted.",
        extracted_data={"title": "Google", "headings": [], "paragraphs": []},
        initial_plan=["Open Google", "Search for AI news", "Extract headlines"],
    )
    generator = ReportGenerator(output_dir=str(tmp_path))
    output = generator.generate_report("task-test", task)

    import io
    reader = PdfReader(io.BytesIO(output))
    text = "\\n".join(page.extract_text() or "" for page in reader.pages)
    assert len(reader.pages) >= 1
    assert "STATUS: FAILED" in text
    assert "TYPE_TEXT" in text
    assert "429 RESOURCE_EXHAUSTED" in text
    assert "private-query" not in text



def test_report_counts_approved_human_interventions(tmp_path):
    task = SimpleNamespace(
        status="COMPLETED",
        goal="Fill the form after user approval",
        steps=[],
        human_approval_count=2,
        created_at=1791928394,
        updated_at=1791928487,
        error=None,
        extracted_data=[],
        initial_plan=[],
    )
    generator = ReportGenerator(output_dir=str(tmp_path))
    output = generator.generate_report("task-approvals", task)

    import io
    reader = PdfReader(io.BytesIO(output))
    report_text = "\\n".join(page.extract_text() or "" for page in reader.pages)

    assert "Human approval steps" in report_text
    assert "2" in report_text
