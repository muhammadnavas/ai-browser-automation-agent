
"""
models/schemas.py
-----------------
Pydantic data models and enumerations for the AI Browser Automation Agent.
"""

from enum import Enum
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
import time
import uuid


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    PENDING          = "pending"
    RUNNING          = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED           = "paused"
    COMPLETED        = "completed"
    FAILED           = "failed"
    CANCELLED        = "cancelled"


class ActionType(str, Enum):
    # Navigation
    NAVIGATE     = "navigate"
    GO_BACK      = "go_back"
    GO_FORWARD   = "go_forward"
    RELOAD       = "reload"

    # Mouse / pointer
    CLICK        = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK  = "right_click"
    HOVER        = "hover"
    DRAG_DROP    = "drag_drop"

    # Form input
    TYPE_TEXT    = "type_text"
    FILL_FIELD   = "fill_field"
    SELECT_OPTION = "select_option"
    CHECK_BOX    = "check_box"
    RADIO_BUTTON = "radio_button"
    PRESS_KEY    = "press_key"
    UPLOAD_FILE  = "upload_file"

    # Generic Form input
    FILL         = "fill"
    SELECT       = "select"
    CHECK        = "check"
    UNCHECK      = "uncheck"
    UPLOAD       = "upload"
    PRESS        = "press"
    FOCUS        = "focus"

    # Scrolling
    SCROLL       = "scroll"
    SCROLL_TO    = "scroll_to"

    # Tab / window management
    NEW_TAB      = "new_tab"
    CLOSE_TAB    = "close_tab"
    SWITCH_TAB   = "switch_tab"

    # Dialog handling
    HANDLE_DIALOG = "handle_dialog"

    # Downloads
    DOWNLOAD_FILE = "download_file"

    # Data / reporting
    EXTRACT      = "extract"
    SCREENSHOT   = "screenshot"

    # Flow control
    WAIT         = "wait"
    REQUEST_APPROVAL = "request_approval"
    FINISH       = "finish"
    ERROR        = "error"
    PLAN_CACHE   = "plan_cache"


class ApprovalType(str, Enum):
    CAPTCHA      = "captcha"
    PAYMENT      = "payment"
    CONFIRMATION = "confirmation"
    CUSTOM       = "custom"


class ReportType(str, Enum):
    CSV = "csv"
    XLSX = "xlsx"
    PDF = "pdf"


class DialogAction(str, Enum):
    """What to do with a browser dialog when HANDLE_DIALOG action fires."""
    ACCEPT  = "accept"
    DISMISS = "dismiss"


# ---------------------------------------------------------------------------
# Action payload
# ---------------------------------------------------------------------------

class BrowserAction(BaseModel):
    """Represents a single browser action decided by the AI Planner."""

    action: ActionType

    # Element targeting
    selector: Optional[str] = None
    element_id: Optional[int] = None

    # Text / value payloads
    text: Optional[str] = None
    url: Optional[str] = None
    value: Optional[str] = None          # for select_option / fill_field

    # Scrolling
    scroll_direction: Optional[str] = "down"
    scroll_amount: Optional[int] = 500

    # Form controls
    checked: Optional[bool] = None       # for check_box action
    radio_name: Optional[str] = None     # for radio_button action
    select_label: Optional[str] = None   # for select_option by label
    select_index: Optional[int] = None   # for select_option by index
    press_enter: Optional[bool] = None   # for type_text action to press enter key

    # Tab management
    tab_index: Optional[int] = None      # for switch_tab / close_tab

    # Dialogs
    dialog_action: Optional[DialogAction] = None   # accept | dismiss
    dialog_response: Optional[str] = None          # text to fill in prompt

    # Downloads
    download_dir: Optional[str] = None
    download_filename: Optional[str] = None

    # File upload
    upload_paths: Optional[List[str]] = None
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    file_base64: Optional[str] = None
    file_size: Optional[int] = None
    folder_name: Optional[str] = None
    directory_path: Optional[str] = None
    neighbor_files: Optional[List[Dict[str, Any]]] = None

    # Source / destination selectors (drag-drop)
    source_selector: Optional[str] = None
    target_selector: Optional[str] = None

    # Keyboard
    key: Optional[str] = None            # for press_key action

    # Wait
    wait_selector: Optional[str] = None
    wait_state: Optional[str] = "visible"
    wait_timeout: Optional[int] = 10000

    # Metadata
    reasoning: Optional[str] = None
    extracted_data: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Execution records
# ---------------------------------------------------------------------------

class AgentStep(BaseModel):
    """Records the result of one executed browser action."""

    step_number: int
    timestamp: float = Field(default_factory=time.time)
    action: ActionType
    description: str
    reasoning: Optional[str] = None
    screenshot_path: Optional[str] = None
    screenshot_url: Optional[str] = None
    url: Optional[str] = None
    extracted_data: Optional[Any] = None
    error: Optional[str] = None
    approval_required: bool = False
    duration_ms: Optional[float] = None


# ---------------------------------------------------------------------------
# Task context memory
# ---------------------------------------------------------------------------

SENSITIVE_FIELD_KEYWORDS = (
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "bearer", "cvv", "pin", "ssn",
    "credit_card", "card_number", "private_key",
)


def _is_sensitive_label(label: str) -> bool:
    """Return True if a field label/key looks like it holds sensitive data."""
    lower = (label or "").lower()
    if any(kw in lower for kw in SENSITIVE_FIELD_KEYWORDS):
        return True
    try:
        from backend.security import is_sensitive_key, is_tier1_key, is_tier2_key
        return is_tier1_key(lower) or is_tier2_key(lower) or is_sensitive_key(lower)
    except Exception:
        return False


class ActionRecord(BaseModel):
    """Compact record of one completed browser action kept in TaskContext."""
    step: int
    action: str
    description: str
    reasoning: Optional[str] = None
    url: Optional[str] = None
    success: bool = True
    error: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)


class TaskContext(BaseModel):
    """
    Structured in-flight memory for a single task execution.

    Stored as a nested field on TaskResponse so it is automatically
    broadcast over WebSocket and persisted to Redis alongside the rest
    of the task state.  Sensitive values (passwords, tokens …) are
    masked before the context is passed to the LLM or written to Redis.
    """

    # Deduped ordered list of every URL the agent has visited
    visited_urls: List[str] = []

    # Full action history (every step, success and failure)
    action_history: List[ActionRecord] = []

    # Data blobs captured by EXTRACT actions
    extracted_snippets: List[Dict[str, Any]] = []

    # Form fields filled by the agent: selector → {label, value}
    # Sensitive values are stored as "********"
    filled_fields: Dict[str, Dict[str, Any]] = {}

    # Rolling log of the last 5 error messages for re-planning
    recent_errors: List[str] = []

    # Mirror of the task-level consecutive-failure counter
    consecutive_failures: int = 0

    # Whether the user has confirmed the form before submission
    form_confirmed: bool = False

    # Flat list of form fields on the current page (set each step)
    current_form_fields: List[Any] = []

    # SHA-256 fingerprint of the last seen DOM state
    last_dom_fingerprint: Optional[str] = None

    # Retrieved user context from My Context (Profile, Documents, Memories)
    retrieved_context: Optional[Dict[str, Any]] = None

    # ----------------------------------------------------------------
    # Mutation helpers
    # ----------------------------------------------------------------

    def record_visit(self, url: str) -> None:
        """Append url to visited_urls (deduped, max 30)."""
        if url and url not in self.visited_urls:
            self.visited_urls.append(url)
            if len(self.visited_urls) > 30:
                self.visited_urls = self.visited_urls[-30:]

    def record_action(
        self,
        step: int,
        action: str,
        description: str,
        reasoning: Optional[str],
        url: Optional[str],
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """Append an ActionRecord and keep the list bounded to 50."""
        self.action_history.append(ActionRecord(
            step=step,
            action=action,
            description=description,
            reasoning=reasoning,
            url=url,
            success=success,
            error=error,
        ))
        if len(self.action_history) > 50:
            self.action_history = self.action_history[-50:]

    def record_fill(self, selector: str, label: str, value: Any) -> None:
        """Record a field fill, masking sensitive values and preventing contact/vault plaintext from leaking."""
        val_str = str(value) if value is not None else ""
        if val_str.startswith("{{") and val_str.endswith("}}"):
            display_value = val_str
        elif _is_sensitive_label(label) or _is_sensitive_label(selector):
            display_value = "********"
        else:
            try:
                from backend.security import (
                    is_sensitive_key,
                    is_tier3_key,
                    contains_tier1_secrets,
                    contains_tier2_identifiers,
                    classify_sensitivity,
                    SensitivityTier,
                )
                if (
                    is_sensitive_key(label)
                    or is_sensitive_key(selector)
                    or is_tier3_key(label)
                    or is_tier3_key(selector)
                    or contains_tier1_secrets(val_str)
                    or contains_tier2_identifiers(val_str)
                    or classify_sensitivity(val_str, key_name=label) == SensitivityTier.TIER_3_CONTACT_PII
                ):
                    display_value = "********"
                else:
                    display_value = value
            except Exception:
                display_value = "********" if _is_sensitive_label(label) or _is_sensitive_label(selector) else value
        self.filled_fields[selector] = {"label": label, "value": display_value}

    def record_error(self, error: str) -> None:
        """Append error to rolling log (max 5)."""
        if error:
            self.recent_errors.append(error)
            if len(self.recent_errors) > 5:
                self.recent_errors = self.recent_errors[-5:]

    def record_extraction(self, data: Dict[str, Any]) -> None:
        """Store an extracted data blob (max 5 retained)."""
        if data:
            self.extracted_snippets.append(data)
            if len(self.extracted_snippets) > 5:
                self.extracted_snippets = self.extracted_snippets[-5:]

    def increment_failures(self) -> None:
        self.consecutive_failures += 1

    def reset_failures(self) -> None:
        self.consecutive_failures = 0

    def confirm_form(self) -> None:
        self.form_confirmed = True

    def clear_transient(self) -> None:
        """Clear page-specific state while retaining useful task memory for the UI/history."""
        self.current_form_fields = []
        self.last_dom_fingerprint = None
        self.form_confirmed = False

    def clear(self) -> None:
        """Wipe all context when starting a fresh execution run."""
        self.visited_urls = []
        self.action_history = []
        self.extracted_snippets = []
        self.filled_fields = {}
        self.recent_errors = []
        self.consecutive_failures = 0
        self.retrieved_context = None
        self.clear_transient()

    def get_retrieved_context_prompt(self) -> str:
        """Return formatted prompt block from retrieved user context."""
        if not self.retrieved_context:
            return ""
        if isinstance(self.retrieved_context, str):
            return self.retrieved_context
        if isinstance(self.retrieved_context, dict):
            return self.retrieved_context.get("formatted_prompt", "")
        return ""

    # ----------------------------------------------------------------
    # LLM prompt helper
    # ----------------------------------------------------------------

    def to_prompt_block(self) -> str:
        """
        Return a compact, token-efficient TASK MEMORY block for injection
        into the LLM prompt.  Keeps total size under ~800 tokens.
        """
        lines: List[str] = []

        # Visited URLs (last 10)
        if self.visited_urls:
            lines.append("Visited URLs (most recent last):")
            for u in self.visited_urls[-10:]:
                lines.append(f"  {u}")

        # Action history (last 12, compact)
        if self.action_history:
            lines.append("Full Action History:")
            for rec in self.action_history[-12:]:
                status = "OK" if rec.success else "FAILED"
                lines.append(
                    f"  Step {rec.step} [{rec.action.upper()}] {status}: {rec.description[:120]}"
                )

        # Filled fields (sanitized values already stored as ****)
        if self.filled_fields:
            lines.append("Fields Filled So Far:")
            for sel, info in self.filled_fields.items():
                lbl = info.get("label") or sel
                val = info.get("value", "")
                lines.append(f"  {lbl}: {str(val)[:80]}")

        # Recent errors
        if self.recent_errors:
            lines.append("Recent Errors (for re-planning):")
            for err in self.recent_errors[-3:]:
                lines.append(f"  ✗ {err[:120]}")

        # Latest extracted snippet
        if self.extracted_snippets:
            import json as _json
            snippet = _json.dumps(self.extracted_snippets[-1], ensure_ascii=False)[:600]
            lines.append(f"Latest Extracted Data: {snippet}")

        # Misc flags
        if self.consecutive_failures:
            lines.append(f"Consecutive Failures: {self.consecutive_failures}")
        if self.form_confirmed:
            lines.append("Form submission: already confirmed by user.")

        return "\n".join(lines) if lines else "(no context yet)"


# ---------------------------------------------------------------------------
# Human approval
# ---------------------------------------------------------------------------

def compute_action_fingerprint(
    task_id: str,
    action_type: str,
    target_url: str = "",
    selector: Optional[str] = None,
    text: Optional[str] = None,
    value: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> str:
    import hashlib
    import hmac
    try:
        from backend.security.auth import SERVER_SECRET
        key = (secret_key or SERVER_SECRET).encode("utf-8")
    except Exception:
        key = (secret_key or "default_secret_fingerprint_key_2026").encode("utf-8")
    payload = f"{task_id}:{action_type}:{target_url}:{selector or ''}:{text or ''}:{value or ''}"
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


class HumanApprovalRequest(BaseModel):
    approval_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    type: ApprovalType
    prompt_message: str
    screenshot_url: Optional[str] = None
    status: str = "pending"   # pending | approved | rejected
    user_input: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    expires_at: float = Field(default_factory=lambda: time.time() + 300.0)
    session_id: Optional[str] = None
    action_fingerprint: Optional[str] = None
    consumed: bool = False

    def is_expired(self) -> bool:
        return time.time() > self.expires_at


# ---------------------------------------------------------------------------
# Task request / response
# ---------------------------------------------------------------------------

class TaskRequest(BaseModel):
    goal: str
    start_url: Optional[str] = None
    show_badges: bool = False
    max_steps: int = Field(default=15, ge=1, le=100)
    auto_approve_captcha: bool = False
    notification_email: Optional[str] = None
    execution_layer: str = "extension"
    headless: bool = True
    # Optional explicit child-agent delegation plan. Empty by default so existing
    # clients retain the original single-agent execution behavior.
    subagent_specs: List[Dict[str, Any]] = Field(default_factory=list, max_length=8)


class ExtensionStatusResponse(BaseModel):
    connected: bool
    client_count: int
    active_tasks: List[str] = []



class TaskResponse(BaseModel):
    task_id: str
    user_id: str = "default_user"
    goal: str
    status: TaskStatus
    created_at: float
    updated_at: float
    current_step: int = 0
    max_steps: int = 15
    steps: List[AgentStep] = []
    extracted_data: List[Any] = []
    approval_request: Optional[HumanApprovalRequest] = None
    # Number of distinct human approval requests explicitly approved by the user.
    # Kept on the task so reports remain accurate after approval_request is cleared.
    human_approval_count: int = 0
    reports: Dict[str, str] = {}
    error: Optional[str] = None
    initial_plan: List[str] = []          # high-level plan generated before execution
    plan: Optional[List[str]] = None      # alias for initial_plan
    plan_status: str = "pending"          # pending | planned | executing | done
    current_plan_step: int = 0            # active milestone index (0-indexed) in initial_plan
    plan_metadata: Optional[Dict[str, Any]] = None  # task understanding (intent, tools, parameters)
    context: TaskContext = Field(default_factory=TaskContext)  # in-flight task memory
    execution_trace: List[Dict[str, Any]] = Field(default_factory=list)  # bounded, redacted execution inspector events



# ---------------------------------------------------------------------------
# Information Extraction Module — Schemas
# ---------------------------------------------------------------------------

class PageCategory(str, Enum):
    """Detected content category of a web page."""
    PRODUCT        = "product"
    NEWS           = "news"
    SEARCH_RESULTS = "search_results"
    CONTACT        = "contact"
    TABLE          = "table"
    IMAGE_GALLERY  = "image_gallery"
    GENERIC        = "generic"


class PageMetadata(BaseModel):
    """Standard page-level metadata present on every extraction result."""
    page_title:        Optional[str] = None
    meta_description:  Optional[str] = None
    canonical_url:     Optional[str] = None
    og_title:          Optional[str] = None
    og_description:    Optional[str] = None
    og_image:          Optional[str] = None
    og_url:            Optional[str] = None
    og_type:           Optional[str] = None
    twitter_title:     Optional[str] = None
    twitter_description: Optional[str] = None
    twitter_image:     Optional[str] = None
    favicon:           Optional[str] = None
    language:          Optional[str] = None
    last_updated:      Optional[str] = None
    keywords:          List[str]     = []


class ProductInfo(BaseModel):
    """Structured data for a product / e-commerce listing page."""
    product_name:        Optional[str]         = None
    brand:               Optional[str]         = None
    description:         Optional[str]         = None
    current_price:       Optional[str]         = None
    original_price:      Optional[str]         = None
    discount_percentage: Optional[float]       = None
    availability:        Optional[str]         = None
    rating:              Optional[float]       = None
    review_count:        Optional[int]         = None
    images:              List[str]             = []
    product_url:         Optional[str]         = None
    category:            Optional[str]         = None
    specifications:      Dict[str, Any]        = {}
    sku:                 Optional[str]         = None
    upc:                 Optional[str]         = None


class NewsArticle(BaseModel):
    """Structured data for a news / editorial article page."""
    headline:         Optional[str]  = None
    summary:          Optional[str]  = None
    article_content:  Optional[str]  = None
    author:           Optional[str]  = None
    publication_date: Optional[str]  = None
    news_source:      Optional[str]  = None
    tags:             List[str]      = []
    featured_image:   Optional[str]  = None
    article_url:      Optional[str]  = None
    section:          Optional[str]  = None


class SearchResult(BaseModel):
    """A single result item from a search results page."""
    position:    int
    title:       Optional[str] = None
    description: Optional[str] = None
    url:         Optional[str] = None
    thumbnail:   Optional[str] = None
    domain:      Optional[str] = None


class ContactInfo(BaseModel):
    """Extracted contact / business information from a page."""
    company_name:     Optional[str] = None
    email_addresses:  List[str]     = []
    phone_numbers:    List[str]     = []
    physical_address: Optional[str] = None
    website:          Optional[str] = None
    social_links:     Dict[str, str] = {}   # {"linkedin": "https://...", ...}
    google_maps_link: Optional[str] = None


class TableCell(BaseModel):
    """Represents a single table cell with optional spanning."""
    value:   str
    rowspan: int = 1
    colspan: int = 1


class TableData(BaseModel):
    """Extracted HTML table with headers, rows, and optional caption."""
    table_index: int
    caption:     Optional[str]           = None
    headers:     List[str]               = []
    rows:        List[List[str]]         = []
    raw_cells:   List[List[TableCell]]   = []   # preserves span metadata
    nested:      bool                    = False


class ImageInfo(BaseModel):
    """Metadata for a single image found on the page."""
    image_url:   Optional[str] = None
    thumbnail:   Optional[str] = None
    alt_text:    Optional[str] = None
    caption:     Optional[str] = None
    page_url:    Optional[str] = None
    width:       Optional[int] = None
    height:      Optional[int] = None


class ExtractionRequest(BaseModel):
    """Request body for the POST /api/extract endpoint."""
    url:      str
    category: Optional[PageCategory] = None   # None = auto-detect
    headless: bool                   = True
    timeout:  int                    = Field(default=30000, ge=1000, le=60000)   # ms
    user_intent: Optional[str]       = None
    explicit_contact_request: bool   = False


class ExtractionResult(BaseModel):
    """
    Top-level standardised extraction response.

    ``data`` holds the category-specific payload:
      - category=product        → ProductInfo dict
      - category=news           → NewsArticle dict
      - category=search_results → list of SearchResult dicts
      - category=contact        → ContactInfo dict
      - category=table          → list of TableData dicts
      - category=image_gallery  → list of ImageInfo dicts
      - category=generic        → empty dict (metadata only)
    """
    url:          str
    page_title:   Optional[str] = None
    category:     PageCategory
    extracted_at: str
    metadata:     PageMetadata                = Field(default_factory=PageMetadata)
    data:         Any                         = None
    errors:       List[str]                   = []


# ---------------------------------------------------------------------------
# Form Discovery Module — Schemas
# ---------------------------------------------------------------------------

class FieldType(str, Enum):
    """Normalised type for every discoverable form field."""
    TEXT        = "text"
    EMAIL       = "email"
    PASSWORD    = "password"
    DROPDOWN    = "dropdown"
    CHECKBOX    = "checkbox"
    RADIO       = "radio"
    TEXTAREA    = "textarea"
    FILE        = "file"
    NUMBER      = "number"
    DATE        = "date"
    TEL         = "tel"
    SEARCH      = "search"
    URL         = "url"
    HIDDEN      = "hidden"
    SUBMIT      = "submit"
    UNKNOWN     = "unknown"


class FieldOption(BaseModel):
    """A single option in a dropdown or radio group."""
    value:    str
    label:    str
    selected: bool = False


class FormField(BaseModel):
    """
    Represents a single discovered form field.

    ``selector`` is always a unique CSS selector ready to use with
    Playwright's ``page.locator()``.
    """
    field_type:    FieldType
    selector:      str                    # unique CSS selector for Playwright
    label:         Optional[str] = None   # human-readable label (inferred)
    name:          Optional[str] = None   # HTML name attribute
    field_id:      Optional[str] = None   # HTML id attribute
    placeholder:   Optional[str] = None
    autocomplete:  Optional[str] = None
    required:      bool          = False
    disabled:      bool          = False
    readonly:      bool          = False
    current_value: Optional[str] = None
    options:       List[FieldOption] = []   # for dropdown / radio / checkbox groups
    group_name:    Optional[str] = None     # for radio/checkbox groups sharing a name
    visible:       bool          = True
    aria_label:    Optional[str] = None
    # Auto-mapping hint — set by discover_and_map()
    mapped_to:     Optional[str] = None     # key in user_data that this field maps to


class DiscoveredForm(BaseModel):
    """Represents one <form> element on the page with all its fields."""
    form_index:       int
    action:           Optional[str] = None   # form action URL
    method:           str           = "get"  # get | post
    fields:           List[FormField]        = []
    submit_selector:  Optional[str] = None   # selector for the submit button
    field_count:      int           = 0


class FormDiscoveryRequest(BaseModel):
    """Request body for POST /api/forms/discover."""
    url:              str
    scope_selector:   Optional[str] = None   # limit scan to a specific container
    headless:         bool          = True
    timeout:          int           = Field(default=30000, ge=1000, le=60000)


class FormDiscoveryResult(BaseModel):
    """
    Top-level response from the Form Discovery engine.

    ``forms`` — all discovered <form> elements.
    ``all_fields`` — flat list across all forms (convenient for single-form pages).
    ``suggested_mapping`` — {field.selector: user_data_key} for auto-fill.
    """
    url:                str
    page_title:         Optional[str]          = None
    discovered_at:      str
    forms:              List[DiscoveredForm]    = []
    all_fields:         List[FormField]        = []
    suggested_mapping:  Dict[str, str]         = {}   # selector → user_data key
    errors:             List[str]              = []


# ---------------------------------------------------------------------------
# Multi-Website Research Module — Schemas
# ---------------------------------------------------------------------------

class ResearchRequest(BaseModel):
    """Request payload for multi-website research and comparison."""
    goal: str
    sources: List[str] = Field(..., min_length=1, description="List of source URLs or search queries")
    criteria: List[str] = Field(default_factory=list, description="Comparison criteria (e.g. price, rating, brand, specs)")
    output_schema: Optional[Dict[str, Any]] = None
    concurrency: int = Field(default=3, ge=1, le=8, description="Max concurrent browser tasks")
    timeout_per_source: int = Field(default=30, ge=5, le=120, description="Timeout in seconds per source")
    export_formats: List[str] = Field(default_factory=lambda: ["csv", "xlsx", "pdf"])
    headless: bool = True


class ExtractedFieldEvidence(BaseModel):
    """Attribution and confidence for an extracted criterion field."""
    value: Any = None
    source_url: str
    extracted_at: str
    field_name: str
    evidence_text: Optional[str] = None
    confidence: float = 1.0
    raw_value: Optional[str] = None
    status: str = "present"  # present | missing | uncertain | invalid


class SourceStatus(BaseModel):
    """Status tracking for an individual research source."""
    url: str
    status: str = "pending"  # pending | running | completed | failed | skipped
    title: Optional[str] = None
    error: Optional[str] = None
    duration_ms: Optional[float] = None
    extracted_fields_count: int = 0


class ComparisonConflict(BaseModel):
    """Explicitly flagged conflict between sources for a specific field."""
    field: str
    values: Dict[str, Any] = Field(default_factory=dict, description="Map of source_url -> observed value")
    description: str


class NormalizedRecord(BaseModel):
    """A deduplicated, normalized entity compared across websites."""
    record_id: str
    name: str
    canonical_key: str
    fields: Dict[str, Any] = Field(default_factory=dict)
    evidence: Dict[str, ExtractedFieldEvidence] = Field(default_factory=dict)
    source_urls: List[str] = Field(default_factory=list)
    is_duplicate_across_sources: bool = False
    conflicts: List[ComparisonConflict] = Field(default_factory=list)


class ComparisonResult(BaseModel):
    """Aggregated comparison matrix across all researched sources."""
    goal: str
    criteria: List[str] = Field(default_factory=list)
    sources: List[SourceStatus] = Field(default_factory=list)
    records: List[NormalizedRecord] = Field(default_factory=list)
    summary: Dict[str, Any] = Field(default_factory=dict)


class ResearchTaskResponse(BaseModel):
    """API response for a research task."""
    task_id: str
    goal: str
    status: TaskStatus
    created_at: float
    updated_at: float
    sources: List[SourceStatus] = Field(default_factory=list)
    comparison: Optional[ComparisonResult] = None
    reports: Dict[str, str] = Field(default_factory=dict)
    error: Optional[str] = None
