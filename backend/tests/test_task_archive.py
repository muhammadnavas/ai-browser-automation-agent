"""Regression tests for durable research task and export storage."""
import tempfile
import unittest
from pathlib import Path

from models.schemas import ComparisonResult, ResearchTaskResponse, SourceStatus, TaskStatus
from services.task_archive import TaskArchive


class ResearchArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.archive = TaskArchive(db_path=str(Path(self.temp_dir.name) / "archive.sqlite3"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_task(self):
        comparison = ComparisonResult(
            goal="Compare products",
            criteria=["price"],
            sources=[SourceStatus(url="https://example.com", status="completed")],
            records=[],
            summary={"total_sources": 1, "total_records": 0},
        )
        return ResearchTaskResponse(
            task_id="research_test_1",
            goal="Compare products",
            status=TaskStatus.COMPLETED,
            created_at=100.0,
            updated_at=101.0,
            sources=comparison.sources,
            comparison=comparison,
            reports={
                "csv": "/api/research/research_test_1/export?format=csv",
                "xlsx": "/api/research/research_test_1/export?format=xlsx",
                "pdf": "/api/research/research_test_1/export?format=pdf",
            },
        )

    def test_research_task_and_comparison_survive_archive_reload(self):
        task = self.make_task()
        self.archive.save_research_task(task)

        restored = self.archive.get_research_task(task.task_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.status, TaskStatus.COMPLETED)
        self.assertIsNotNone(restored.comparison)
        self.assertEqual(restored.comparison.summary["total_sources"], 1)
        self.assertEqual(self.archive.list_research_tasks()[0].task_id, task.task_id)

    def test_each_research_export_format_is_persistent(self):
        for fmt, data in (("csv", b"a,b\n1,2\n"), ("xlsx", b"xlsx-bytes"), ("pdf", b"%PDF-test")):
            self.archive.save_research_export("research_test_1", fmt, data)
            self.assertEqual(self.archive.get_research_export("research_test_1", fmt), data)

    def test_delete_removes_research_task_and_exports(self):
        task = self.make_task()
        self.archive.save_research_task(task)
        self.archive.save_research_export(task.task_id, "csv", b"a,b")
        self.archive.delete_task(task.task_id)

        self.assertIsNone(self.archive.get_research_task(task.task_id))
        self.assertIsNone(self.archive.get_research_export(task.task_id, "csv"))


if __name__ == "__main__":
    unittest.main()
