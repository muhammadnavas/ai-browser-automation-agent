"""
browser/form_discovery.py
--------------------------
Automatic Form Field Discovery Engine for the AI Browser Automation Agent.

The ``FormDiscovery`` class scans the live DOM of a Playwright Page and
returns a fully-structured ``FormDiscoveryResult`` describing every
form field it finds — with no manual configuration required.

Discovered fields include:
  - Text, email, password, number, tel, search, url, date inputs
  - Textareas
  - Dropdowns (<select>) with all option values
  - Checkboxes (individual and groups)
  - Radio button groups (deduplicated by name)
  - File upload inputs
  - Hidden fields
  - Submit buttons

Each field includes:
  - A unique CSS selector ready for Playwright ``page.locator()``
  - An inferred human-readable label (8-source priority chain)
  - name, id, placeholder, autocomplete, required, disabled, readonly
  - Current value / checked state
  - All options (for dropdowns and radio groups)

The ``discover_and_map(page, user_data)`` method additionally performs
fuzzy keyword matching to suggest which user_data key each field should
receive, enabling zero-config form filling.

Usage::

    from browser.form_discovery import FormDiscovery

    discovery = FormDiscovery()
    result = await discovery.discover(page)
    print(result.model_dump_json(indent=2))

    # Or map to user data automatically:
    result = await discovery.discover_and_map(page, {
        "email": "jane@example.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "phone": "+1-555-0100",
        "password": "s3cur3P@ss",
    })
    for field in result.all_fields:
        print(field.selector, "→", field.mapped_to)
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import Page

from models.schemas import (
    DiscoveredForm,
    FieldOption,
    FieldType,
    FormDiscoveryResult,
    FormField,
)

logger = logging.getLogger("browser.form_discovery")


# ---------------------------------------------------------------------------
# Input type → FieldType mapping
# ---------------------------------------------------------------------------

_INPUT_TYPE_MAP: Dict[str, FieldType] = {
    "text":     FieldType.TEXT,
    "email":    FieldType.EMAIL,
    "password": FieldType.PASSWORD,
    "checkbox": FieldType.CHECKBOX,
    "radio":    FieldType.RADIO,
    "file":     FieldType.FILE,
    "number":   FieldType.NUMBER,
    "date":     FieldType.DATE,
    "datetime-local": FieldType.DATE,
    "month":    FieldType.DATE,
    "week":     FieldType.DATE,
    "time":     FieldType.DATE,
    "tel":      FieldType.TEL,
    "search":   FieldType.SEARCH,
    "url":      FieldType.URL,
    "hidden":   FieldType.HIDDEN,
    "submit":   FieldType.SUBMIT,
    "button":   FieldType.SUBMIT,
    "image":    FieldType.SUBMIT,
    "reset":    FieldType.UNKNOWN,
    "range":    FieldType.NUMBER,
    "color":    FieldType.UNKNOWN,
}


# ---------------------------------------------------------------------------
# Auto-mapping keyword rules
# Each entry: (keywords_to_match, user_data_key)
# Matched against the lowercased label/name/placeholder of the field.
# ---------------------------------------------------------------------------

_MAPPING_RULES: List[Tuple[List[str], str]] = [
    (["first name", "firstname", "given name", "fname"],           "first_name"),
    (["last name", "lastname", "surname", "family name", "lname"], "last_name"),
    (["full name", "your name", "contact name", "name"],           "name"),
    (["email", "e-mail", "email address"],                          "email"),
    (["phone", "mobile", "cell", "telephone", "tel", "contact number"], "phone"),
    (["password", "pass word", "passcode"],                         "password"),
    (["confirm password", "retype password", "repeat password",
      "password confirmation", "confirm pass"],                     "confirm_password"),
    (["username", "user name", "login", "user id", "userid"],       "username"),
    (["address", "street", "street address", "address line 1",
      "addr", "addr1"],                                             "address"),
    (["address line 2", "addr2", "apartment", "suite", "apt"],      "address2"),
    (["city", "town", "suburb"],                                    "city"),
    (["state", "province", "region"],                               "state"),
    (["zip", "postal", "postcode", "zip code", "postal code"],      "zip_code"),
    (["country"],                                                   "country"),
    (["company", "organization", "organisation", "employer",
      "business name"],                                             "company"),
    (["job title", "position", "role", "designation"],              "job_title"),
    (["website", "url", "homepage", "web address"],                 "website"),
    (["message", "notes", "comments", "enquiry", "inquiry",
      "description", "details", "feedback"],                        "message"),
    (["subject", "topic", "regarding"],                             "subject"),
    (["date of birth", "dob", "birthday", "birth date"],            "date_of_birth"),
    (["gender", "sex"],                                             "gender"),
    (["age"],                                                       "age"),
    (["cv", "resume", "curriculum vitae"],                          "resume"),
    (["file", "attachment", "upload", "document"],                  "file"),
]


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class FormDiscovery:
    """
    Automatic form field discovery and user-data mapping engine.

    Args:
        include_hidden:  Include ``<input type="hidden">`` fields (default False).
        include_submit:  Include submit buttons in results (default False).
    """

    def __init__(
        self,
        include_hidden: bool = False,
        include_submit: bool = False,
    ) -> None:
        self.include_hidden = include_hidden
        self.include_submit = include_submit

    # ------------------------------------------------------------------
    # Primary entry-points
    # ------------------------------------------------------------------

    async def discover(
        self,
        page: Page,
        scope_selector: Optional[str] = None,
    ) -> FormDiscoveryResult:
        """
        Discover all form fields on the active Playwright page.

        Args:
            page:             Active Playwright Page (already navigated).
            scope_selector:   Optional CSS selector to limit the scan to a
                              specific container element.

        Returns:
            A ``FormDiscoveryResult`` with all discovered forms and fields.
        """
        errors: List[str] = []

        try:
            page_title: str = await page.title()
        except Exception:
            page_title = ""

        try:
            raw: Dict[str, Any] = await page.evaluate(
                _JS_DISCOVER_FORMS,
                scope_selector or "null",
            )
        except Exception as exc:
            logger.error(f"Form discovery JS failed: {exc}")
            return FormDiscoveryResult(
                url          = page.url,
                page_title   = page_title,
                discovered_at = _now(),
                errors        = [f"JS evaluation failed: {exc}"],
            )

        forms:      List[DiscoveredForm] = []
        all_fields: List[FormField]      = []

        for fi, raw_form in enumerate(raw.get("forms", [])):
            form_fields: List[FormField] = []
            seen_radio_groups: set = set()

            for raw_field in raw_form.get("fields", []):
                field = _parse_raw_field(
                    raw_field,
                    form_index     = fi,
                    include_hidden = self.include_hidden,
                    include_submit = self.include_submit,
                    seen_radio_groups = seen_radio_groups,
                )
                if field:
                    form_fields.append(field)
                    all_fields.append(field)

            submit_sel = raw_form.get("submit_selector")
            forms.append(DiscoveredForm(
                form_index      = fi,
                action          = raw_form.get("action") or None,
                method          = (raw_form.get("method") or "get").lower(),
                fields          = form_fields,
                submit_selector = submit_sel,
                field_count     = len(form_fields),
            ))

        logger.info(
            f"Discovered {len(forms)} form(s), {len(all_fields)} field(s) on {page.url}"
        )

        return FormDiscoveryResult(
            url           = page.url,
            page_title    = page_title or None,
            discovered_at = _now(),
            forms         = forms,
            all_fields    = all_fields,
            errors        = errors,
        )

    async def discover_and_map(
        self,
        page: Page,
        user_data: Dict[str, Any],
        scope_selector: Optional[str] = None,
    ) -> FormDiscoveryResult:
        """
        Discover all form fields and automatically map them to ``user_data``.

        Each ``FormField.mapped_to`` will be set to the matching
        ``user_data`` key (or ``None`` if no match found).
        ``FormDiscoveryResult.suggested_mapping`` will contain a
        ``{selector: user_data_key}`` dict ready for ``FormFiller``.

        Args:
            page:           Active Playwright Page.
            user_data:      Dict of user data to map fields to.
            scope_selector: Optional container selector.

        Returns:
            ``FormDiscoveryResult`` with ``mapped_to`` and
            ``suggested_mapping`` populated.
        """
        result = await self.discover(page, scope_selector)
        user_keys = set(user_data.keys())
        mapping:  Dict[str, str] = {}

        for field in result.all_fields:
            if field.field_type in (FieldType.HIDDEN, FieldType.SUBMIT):
                continue

            matched_key = _auto_map_field(field, user_keys)
            if matched_key:
                field.mapped_to = matched_key
                mapping[field.selector] = matched_key

        result.suggested_mapping = mapping

        # Update fields in forms too (they share the same objects via ref)
        for form in result.forms:
            for field in form.fields:
                if field.selector in mapping:
                    field.mapped_to = mapping[field.selector]

        return result


# ---------------------------------------------------------------------------
# Raw field parser
# ---------------------------------------------------------------------------

def _parse_raw_field(
    raw: Dict[str, Any],
    form_index: int,
    include_hidden: bool,
    include_submit: bool,
    seen_radio_groups: set,
) -> Optional[FormField]:
    """Convert a raw JS field dict into a ``FormField`` Pydantic model."""

    tag      = (raw.get("tag") or "").lower()
    raw_type = (raw.get("type") or "text").lower()

    # Resolve FieldType
    if tag == "textarea" or raw_type == "contenteditable":
        field_type = FieldType.TEXTAREA
    elif tag == "select" or raw_type == "dropdown":
        field_type = FieldType.DROPDOWN
    elif tag == "button" or raw_type == "submit":
        field_type = FieldType.SUBMIT
    else:
        field_type = _INPUT_TYPE_MAP.get(raw_type, FieldType.TEXT)

    # Filter based on options
    if field_type == FieldType.HIDDEN and not include_hidden:
        return None
    if field_type == FieldType.SUBMIT and not include_submit:
        return None

    # Deduplicate radio groups — keep only first occurrence per group name
    if field_type == FieldType.RADIO:
        group = raw.get("name") or raw.get("id") or ""
        if group in seen_radio_groups:
            return None
        if group:
            seen_radio_groups.add(group)

    # Build options list
    options: List[FieldOption] = []
    for opt in raw.get("options", []):
        options.append(FieldOption(
            value    = str(opt.get("value", "")),
            label    = str(opt.get("label") or opt.get("value", "")),
            selected = bool(opt.get("selected", False)),
        ))

    # Infer label
    label = _pick_label(raw)

    raw_val = raw.get("value") or None
    raw_ph = raw.get("placeholder") or None
    raw_aria = raw.get("aria_label") or None

    try:
        from backend.security import is_auth_field, redact_text
        if is_auth_field(raw):
            current_val = ""
        elif raw_val:
            current_val = redact_text(str(raw_val), mask_tier3=True, preserve_public_contacts=True)
        else:
            current_val = None

        if label:
            label = redact_text(str(label), mask_tier3=True, preserve_public_contacts=True)
        if raw_ph:
            raw_ph = redact_text(str(raw_ph), mask_tier3=True, preserve_public_contacts=True)
        if raw_aria:
            raw_aria = redact_text(str(raw_aria), mask_tier3=True, preserve_public_contacts=True)
    except Exception:
        current_val = "" if (raw.get("type") or "").lower() == "password" else raw_val

    return FormField(
        field_type    = field_type,
        selector      = raw.get("selector", ""),
        label         = label,
        name          = raw.get("name") or None,
        field_id      = raw.get("id") or None,
        placeholder   = raw_ph,
        autocomplete  = raw.get("autocomplete") or None,
        required      = bool(raw.get("required", False)),
        disabled      = bool(raw.get("disabled", False)),
        readonly      = bool(raw.get("readonly", False)),
        current_value = current_val,
        options       = options,
        group_name    = raw.get("name") or None if field_type in (
            FieldType.RADIO, FieldType.CHECKBOX
        ) else None,
        visible       = bool(raw.get("visible", True)),
        aria_label    = raw_aria,
    )


# ---------------------------------------------------------------------------
# Label inference
# ---------------------------------------------------------------------------

def _pick_label(raw: Dict[str, Any]) -> Optional[str]:
    """
    Return the best human-readable label for a field using an 8-source
    priority chain.
    """
    sources = [
        raw.get("label_for"),       # 1. <label for="id">
        raw.get("aria_label"),      # 2. aria-label attribute
        raw.get("placeholder"),     # 3. placeholder
        raw.get("label_parent"),    # 4. wrapping <label> text
        raw.get("label_preceding"), # 5. nearest preceding <label> sibling
        raw.get("label_sibling"),   # 6. nearest preceding text sibling
        _titlecase(raw.get("name")),  # 7. name attr titlecased
        _titlecase(raw.get("id")),    # 8. id attr titlecased
    ]
    for s in sources:
        if s and str(s).strip():
            return str(s).strip()
    return None


def _titlecase(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    # Convert snake_case / camelCase / kebab-case to Title Case
    val = re.sub(r"[-_]", " ", val)
    val = re.sub(r"([a-z])([A-Z])", r"\1 \2", val)
    return val.strip().title()


# ---------------------------------------------------------------------------
# Auto-mapping
# ---------------------------------------------------------------------------

def _auto_map_field(field: FormField, user_keys: set) -> Optional[str]:
    """
    Fuzzy-match a field's label/name/placeholder to a user_data key.

    Returns the matching user_data key, or None.
    """
    # Build candidate strings from highest to lowest signal quality
    candidates = [
        (field.label         or "").lower(),
        (field.name          or "").lower().replace("-", " ").replace("_", " "),
        (field.placeholder   or "").lower(),
        (field.autocomplete  or "").lower(),
        _titlecase(field.field_id or "").lower() if field.field_id else "",
    ]
    combined = " | ".join(c for c in candidates if c)

    # Try autocomplete attribute first — it's the most reliable signal
    ac = (field.autocomplete or "").lower()
    ac_map = {
        "email":         "email",
        "new-password":  "password",
        "current-password": "password",
        "given-name":    "first_name",
        "family-name":   "last_name",
        "name":          "name",
        "tel":           "phone",
        "street-address":"address",
        "address-line1": "address",
        "address-line2": "address2",
        "address-level2":"city",
        "address-level1":"state",
        "postal-code":   "zip_code",
        "country":       "country",
        "country-name":  "country",
        "organization":  "company",
        "username":      "username",
        "bday":          "date_of_birth",
        "url":           "website",
    }
    if ac in ac_map:
        key = ac_map[ac]
        if key in user_keys:
            return key

    # Keyword rule matching
    for keywords, data_key in _MAPPING_RULES:
        for kw in keywords:
            if kw in combined:
                if data_key in user_keys:
                    return data_key
                # Try close variants
                for variant in [data_key, data_key.replace("_", ""), data_key.replace("_", "-")]:
                    if variant in user_keys:
                        return variant

    # Field type heuristics as last resort
    if field.field_type == FieldType.EMAIL and "email" in user_keys:
        return "email"
    if field.field_type == FieldType.PASSWORD and "password" in user_keys:
        return "password"
    if field.field_type == FieldType.TEL and "phone" in user_keys:
        return "phone"
    if field.field_type == FieldType.FILE and "file" in user_keys:
        return "file"
    if field.field_type == FieldType.FILE and "resume" in user_keys:
        return "resume"

    return None


# ---------------------------------------------------------------------------
# JavaScript — runs in browser context
# ---------------------------------------------------------------------------

_JS_DISCOVER_FORMS = """
(scopeSelector) => {
    // ── Helpers ──────────────────────────────────────────────────────────

    // Build a unique CSS selector for any element
    const uniqueSelector = (el) => {
        if (el.id) return `#${CSS.escape(el.id)}`;
        if (el.name) {
            const tag = el.tagName.toLowerCase();
            const byName = document.querySelectorAll(`${tag}[name="${el.name}"]`);
            if (byName.length === 1) return `${tag}[name="${CSS.escape(el.name)}"]`;
            const idx = Array.from(byName).indexOf(el);
            return `${tag}[name="${CSS.escape(el.name)}"]:nth-of-type(${idx + 1})`;
        }
        // Walk up and build a path
        const path = [];
        let node = el;
        while (node && node !== document.body) {
            let seg = node.tagName.toLowerCase();
            if (node.id) { seg = `#${CSS.escape(node.id)}`; path.unshift(seg); break; }
            const siblings = node.parentElement
                ? Array.from(node.parentElement.children).filter(c => c.tagName === node.tagName)
                : [];
            if (siblings.length > 1) seg += `:nth-of-type(${siblings.indexOf(node) + 1})`;
            path.unshift(seg);
            node = node.parentElement;
        }
        return path.join(' > ');
    };

    // Infer label text from various DOM sources
    const inferLabel = (el) => {
        // 1. <label for="id">
        if (el.id) {
            const lbl = document.querySelector(`label[for="${el.id}"]`);
            if (lbl) return { label_for: lbl.innerText.trim() };
        }
        // 2. aria-label
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel) return { aria_label: ariaLabel.trim() };

        // 3. placeholder (handled separately as field)

        // 4. Wrapping <label>
        const parentLabel = el.closest('label');
        if (parentLabel) {
            const clone = parentLabel.cloneNode(true);
            clone.querySelectorAll('input,select,textarea').forEach(c => c.remove());
            const txt = clone.innerText.trim();
            if (txt) return { label_parent: txt };
        }

        // 5. Preceding <label> sibling
        let prev = el.previousElementSibling;
        while (prev) {
            if (prev.tagName === 'LABEL') return { label_preceding: prev.innerText.trim() };
            prev = prev.previousElementSibling;
        }

        // 6. aria-labelledby
        const labelledBy = el.getAttribute('aria-labelledby');
        if (labelledBy) {
            const lbl = document.getElementById(labelledBy);
            if (lbl) return { label_sibling: lbl.innerText.trim() };
        }

        // 7-8 handled in Python from name/id
        return {};
    };

    // Extract <select> options or custom combobox/listbox options
    const extractOptions = (selectEl) => {
        if (selectEl.tagName.toLowerCase() === 'select') {
            return Array.from(selectEl.options).map(opt => ({
                value:    opt.value,
                label:    opt.text.trim(),
                selected: opt.selected,
            }));
        }
        const opts = selectEl.querySelectorAll('[role="option"], li, option');
        if (opts.length > 0) {
            return Array.from(opts).map(opt => ({
                value: opt.getAttribute('data-value') || opt.getAttribute('value') || opt.innerText.trim(),
                label: opt.innerText.trim(),
                selected: opt.getAttribute('aria-selected') === 'true' || opt.classList.contains('selected'),
            }));
        }
        return [];
    };

    // Extract radio group options (all inputs with the same name)
    const extractRadioOptions = (radioEl) => {
        const name = radioEl.name;
        if (!name) return [{ value: radioEl.value, label: radioEl.value, selected: radioEl.checked }];
        return Array.from(document.querySelectorAll(`input[type="radio"][name="${name}"]`)).map(r => {
            const lblObj = inferLabel(r);
            const lbl = lblObj.label_for || lblObj.aria_label || lblObj.label_parent ||
                        lblObj.label_preceding || r.value || '';
            return { value: r.value, label: lbl, selected: r.checked };
        });
    };

    // Find submit button for a form
    const findSubmit = (formEl) => {
        // type=submit inside form
        const btn = formEl
            ? (formEl.querySelector('[type="submit"]') ||
               formEl.querySelector('button:not([type="button"])') ||
               formEl.querySelector('button'))
            : (document.querySelector('[type="submit"]') ||
               document.querySelector('button[class*="submit"]') ||
               document.querySelector('button[id*="submit"]') ||
               Array.from(document.querySelectorAll('button')).find(b =>
                   /submit|send|register|sign.?up|log.?in|continue|next/i.test(b.innerText)
               ));
        return btn ? uniqueSelector(btn) : null;
    };

    // ── Scope ─────────────────────────────────────────────────────────────
    const root = (scopeSelector && scopeSelector !== 'null')
        ? document.querySelector(scopeSelector) || document
        : document;

    const FIELD_SELECTOR = 'input:not([type="button"]):not([type="image"]):not([type="reset"]), select, textarea, button, [contenteditable="true"], [role="combobox"], [role="listbox"]';

    // ── Discover forms ────────────────────────────────────────────────────
    const formEls = Array.from(root.querySelectorAll('form'));
    const discoveredForms = [];

    const processFields = (container, isFakeForm) => {
        const fields = [];
        container.querySelectorAll(FIELD_SELECTOR).forEach(el => {
            if (isFakeForm && el.closest('form')) return;  // skip — it belongs to a real form

            const tag  = el.tagName.toLowerCase();
            let type = (el.getAttribute('type') || (tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : 'text')).toLowerCase();
            if (el.getAttribute('contenteditable') === 'true') {
                type = 'contenteditable';
            }
            if (el.getAttribute('role') === 'combobox' || el.getAttribute('role') === 'listbox') {
                type = 'dropdown';
            }
            const labelData = inferLabel(el);

            const cs = window.getComputedStyle(el);
            const visible = cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0' && el.offsetWidth > 0 && el.offsetHeight > 0;

            let options = [];
            if (tag === 'select' || type === 'dropdown') options = extractOptions(el);
            if (type === 'radio') options = extractRadioOptions(el);

            fields.push({
                tag,
                type,
                selector:        uniqueSelector(el),
                id:              el.id || null,
                name:            el.getAttribute('name') || null,
                placeholder:     el.getAttribute('placeholder') || null,
                autocomplete:    el.getAttribute('autocomplete') || null,
                required:        el.required || el.getAttribute('aria-required') === 'true',
                disabled:        el.disabled,
                readonly:        el.readOnly || false,
                value:           (type === 'checkbox' || type === 'radio') ? el.checked ? el.value : '' : el.value || null,
                options,
                visible,
                aria_label:      el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || null,
                ...labelData,
            });
        });
        return fields;
    };

    if (formEls.length > 0) {
        formEls.forEach((formEl, idx) => {
            discoveredForms.push({
                action:          formEl.getAttribute('action') || null,
                method:          formEl.getAttribute('method') || 'get',
                submit_selector: findSubmit(formEl),
                fields:          processFields(formEl, false),
            });
        });
    } else {
        // No <form> tags — treat whole page as one form
        discoveredForms.push({
            action:          null,
            method:          'post',
            submit_selector: findSubmit(null),
            fields:          processFields(root, true),
        });
    }

    return { forms: discoveredForms };
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_compact_dict(result: FormDiscoveryResult) -> Dict[str, Any]:
    """Convert FormDiscoveryResult into a compact structured representation for the LLM."""
    compact_forms = []
    for fi, form in enumerate(result.forms):
        fields_list = []
        for field in form.fields:
            # We skip hidden fields, but keep visible ones
            if field.field_type == FieldType.HIDDEN or not field.visible:
                continue
            field_dict = {
                "id": field.field_id or field.selector,
                "selector": field.selector,
                "label": field.label or field.name or field.field_id or "",
                "type": field.field_type.value,
                "required": field.required,
            }
            if field.placeholder:
                field_dict["placeholder"] = field.placeholder
            if field.options:
                field_dict["options"] = [opt.label for opt in field.options]
            if field.current_value:
                field_dict["value"] = field.current_value
            if field.mapped_to:
                field_dict["mapped_to"] = field.mapped_to
            fields_list.append(field_dict)
            
        compact_forms.append({
            "form_id": f"form_{fi + 1}",
            "submit_selector": form.submit_selector,
            "fields": fields_list,
        })
        
    return {
        "page_type": "form" if compact_forms and any(f["fields"] for f in compact_forms) else "generic",
        "url": result.url,
        "title": result.page_title,
        "forms": compact_forms,
    }
