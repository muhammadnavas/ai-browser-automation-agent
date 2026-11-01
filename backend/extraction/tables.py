"""
extraction/tables.py
--------------------
Extracts all HTML tables from a Playwright Page into structured ``TableData``
objects that preserve headers, rows, cell spanning metadata, captions, and
nested table detection.

For each <table>:
  - Extracts <caption> text
  - Reads <thead><th> or first <tr><th> as column headers
  - Reads all <tr><td>/<th> as rows
  - Captures rowspan / colspan on each cell
  - Flags tables that contain nested <table> elements
"""

import logging
import re
from typing import Any, Dict, List, Optional

from playwright.async_api import Page
from models.schemas import TableData, TableCell

logger = logging.getLogger("extraction.tables")


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

async def extract_tables(page: Page) -> List[TableData]:
    """
    Extract all HTML tables from the current Playwright page.

    Args:
        page: An active Playwright ``Page`` (already navigated).

    Returns:
        List of ``TableData`` objects, one per <table> element.
        Tables that appear to be layout-only (no <th> and single column)
        are filtered out.
    """
    try:
        raw_tables: List[Dict[str, Any]] = await page.evaluate(_JS_EXTRACT_TABLES)
    except Exception as exc:
        logger.error(f"Table extraction JS failed: {exc}")
        return []

    result: List[TableData] = []
    for i, raw in enumerate(raw_tables):
        table = _parse_raw_table(i, raw)
        if table:
            result.append(table)

    logger.debug(f"Extracted {len(result)} tables from {page.url}")
    return result


# ---------------------------------------------------------------------------
# Raw table parser
# ---------------------------------------------------------------------------

def _parse_raw_table(index: int, raw: Dict[str, Any]) -> Optional[TableData]:
    """Convert a raw JS table dict into a ``TableData`` Pydantic model."""
    caption = _clean(raw.get("caption"))
    headers: List[str] = [_clean(h) or "" for h in raw.get("headers", [])]
    nested: bool = raw.get("nested", False)

    raw_row_cells: List[List[Dict]] = raw.get("rows", [])

    # Build typed raw_cells
    typed_rows: List[List[TableCell]] = []
    plain_rows: List[List[str]] = []

    for raw_row in raw_row_cells:
        typed_row: List[TableCell] = []
        plain_row: List[str] = []
        for cell in raw_row:
            tc = TableCell(
                value   = _clean(cell.get("value", "")) or "",
                rowspan = int(cell.get("rowspan", 1) or 1),
                colspan = int(cell.get("colspan", 1) or 1),
            )
            typed_row.append(tc)
            plain_row.append(tc.value)
        typed_rows.append(typed_row)
        plain_rows.append(plain_row)

    # Filter trivial / layout tables: no headers + single column
    total_cols = max((len(r) for r in plain_rows), default=0)
    if not headers and total_cols <= 1:
        return None

    return TableData(
        table_index = index,
        caption     = caption,
        headers     = headers,
        rows        = plain_rows,
        raw_cells   = typed_rows,
        nested      = nested,
    )


# ---------------------------------------------------------------------------
# JavaScript executed in the browser context
# ---------------------------------------------------------------------------

_JS_EXTRACT_TABLES = """
() => {
    const allTables = Array.from(document.querySelectorAll('table'));
    const results = [];

    allTables.forEach(table => {
        // Skip tables nested inside another table that we will process separately
        // (only process top-level + first-level nested)
        const parentTable = table.parentElement
            ? table.parentElement.closest('table')
            : null;

        const caption = table.caption ? table.caption.innerText.trim() : null;

        // Detect if this table has nested tables
        const nested = table.querySelector('table') !== null;

        // ----- Headers -----
        let headers = [];
        const thead = table.querySelector('thead');

        if (thead) {
            const thEls = thead.querySelectorAll('th');
            if (thEls.length > 0) {
                headers = Array.from(thEls).map(th => th.innerText.trim());
            }
        }

        // If no <thead>, check if first <tr> is all <th>
        if (headers.length === 0) {
            const firstRow = table.querySelector('tr');
            if (firstRow) {
                const thEls = firstRow.querySelectorAll('th');
                if (thEls.length > 0 && thEls.length === firstRow.querySelectorAll('td, th').length) {
                    headers = Array.from(thEls).map(th => th.innerText.trim());
                }
            }
        }

        // ----- Rows -----
        const rows = [];
        const tbody = table.querySelector('tbody') || table;
        const trEls = tbody.querySelectorAll('tr');

        trEls.forEach((tr, rowIdx) => {
            // Skip the header row we already parsed
            const isHeaderRow = (rowIdx === 0 && headers.length > 0 &&
                tr.querySelectorAll('th').length === tr.querySelectorAll('td, th').length);
            if (isHeaderRow && !thead) return;

            const cells = Array.from(tr.querySelectorAll('td, th'));
            if (cells.length === 0) return;

            const rowData = cells.map(cell => ({
                value:   cell.innerText.trim(),
                rowspan: parseInt(cell.getAttribute('rowspan') || '1', 10),
                colspan: parseInt(cell.getAttribute('colspan') || '1', 10),
            }));
            rows.push(rowData);
        });

        if (rows.length === 0) return;

        results.push({ caption, headers, rows, nested });
    });

    return results;
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean(val: Any) -> Optional[str]:
    if val is None:
        return None
    text = re.sub(r"\s+", " ", str(val)).strip()
    return text if text else None
