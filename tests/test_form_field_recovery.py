"""Regression tests for form-field recovery with typed discovery models."""

import pytest

from browser.recovery import _form_field_value, _verify_recovered_action
from models.schemas import ActionType, BrowserAction, FieldType, FormField


class _FakeLocator:
    def __init__(self, value=None, exists=True):
        self.value = value
        self.exists = exists
        self.first = self

    async def count(self):
        return 1 if self.exists else 0

    async def is_visible(self):
        return self.exists

    async def input_value(self, timeout=None):
        return self.value


class _FakePage:
    def __init__(self, expected_value):
        self.expected_value = expected_value
        self.requested_labels = []

    def locator(self, selector):
        # Simulate a stale nth-of-type selector after the form rerenders.
        return _FakeLocator(exists=False)

    def get_by_label(self, label, exact=False):
        self.requested_labels.append(label)
        return _FakeLocator(value=self.expected_value, exists=(label == "Student Name"))


def test_form_field_value_supports_pydantic_model_and_dict():
    field = FormField(
        field_type=FieldType.TEXT,
        selector="input:nth-of-type(2)",
        label="Student Name",
        field_id="student-name",
    )

    assert _form_field_value(field, "label") == "Student Name"
    assert _form_field_value(field, "field_id") == "student-name"
    assert _form_field_value({"label": "Email"}, "label") == "Email"
    assert _form_field_value(field, "missing", "fallback") == "fallback"


@pytest.mark.asyncio
async def test_recovery_verification_uses_semantic_locator_after_rerender():
    expected = "Navas"
    field = FormField(
        field_type=FieldType.TEXT,
        selector="input:nth-of-type(2)",
        label="Student Name",
        field_id="student-name",
    )
    page = _FakePage(expected)
    action = BrowserAction(
        action=ActionType.FILL,
        selector="input:nth-of-type(2)",
        text=expected,
    )

    verified, message = await _verify_recovered_action(
        page,
        action,
        controller=None,
        form_fields=[field],
    )

    assert verified is True
    assert "label locator" in message
    assert "Student Name" in page.requested_labels


@pytest.mark.asyncio
async def test_recovery_verification_accepts_legacy_dict_field_metadata():
    expected = "navas@example.com"
    page = _FakePage(expected)
    action = BrowserAction(
        action=ActionType.FILL,
        selector="#email",
        text=expected,
    )

    verified, message = await _verify_recovered_action(
        page,
        action,
        controller=None,
        form_fields=[{
            "selector": "#email",
            "label": "Student Email",
            "field_id": "email",
        }],
    )

    assert verified is True
    assert "label locator" in message
