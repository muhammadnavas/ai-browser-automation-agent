import json

from services.task_file_logger import TaskFileLogger


def test_task_logger_writes_readable_and_structured_events(tmp_path):
    logger = TaskFileLogger("task-123", root=tmp_path)
    logger.event("task_started", {"goal": "open a page"})
    logger.dom_snapshot(2, {"url": "https://example.com", "elements": [{"tag": "button", "text": "Continue"}]})
    logger.plan(1, ["Inspect page", "Click Continue"])

    task_dir = tmp_path / "task-123"
    assert (task_dir / "task.log").exists()
    events = [json.loads(line) for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(event["event"] == "task_started" for event in events)
    assert (task_dir / "dom" / "step_0002.json").exists()
    assert (task_dir / "plans" / "v01.json").exists()
    assert "TASK_STARTED" in (task_dir / "task.log").read_text(encoding="utf-8")


def test_task_logger_redacts_password_and_secret_fields(tmp_path):
    logger = TaskFileLogger("task-secret", root=tmp_path)
    logger.dom_snapshot(1, {
        "elements": [
            {"tag": "input", "type": "password", "value": "do-not-write-this"},
            {"tag": "input", "type": "text", "value": "ordinary text"},
        ],
        "api_token": "do-not-write-this-either",
    })

    raw = (tmp_path / "task-secret" / "dom" / "step_0001.json").read_text(encoding="utf-8")
    assert "do-not-write-this" not in raw
    assert "[REDACTED]" in raw
    assert "ordinary text" in raw
