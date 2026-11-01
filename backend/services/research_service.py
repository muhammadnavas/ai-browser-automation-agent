"""
services/research_service.py
----------------------------
Core Multi-Website Research Orchestrator.

Integrates:
- BrowserPool for warm browser context acquisition and bounded concurrency.
- TabManager for page/tab execution.
- URL validation and navigation route guards for SSRF/redirect security.
- InfoExtractor and DOM criteria parser for deterministic structured extraction.
- Conservative normalization and deduplication across sources.
- Multi-format report export (CSV, XLSX, PDF).
- TaskManager lifecycle and WebSocket progress broadcasting.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from urllib.parse import quote_plus

from models.schemas import (
    ComparisonResult,
    ExtractionResult,
    ResearchRequest,
    ResearchTaskResponse,
    SourceStatus,
    TaskResponse,
    TaskStatus,
    TaskContext,
)
from browser.pool import get_browser_pool
from browser.extractor import InfoExtractor
from browser.waiter import settle_page
from browser.security import validate_url_security, install_navigation_guard
from extraction.listing import extract_listing_records
from services.research_normalization import deduplicate_and_compare
from services.research_export_service import ResearchExportService
from services.task_archive import task_archive

logger = logging.getLogger("services.research_service")


def resolve_source_target(source: str) -> Tuple[str, bool]:
    """
    Detect search instructions vs URLs.
    Supports 'search: <query>', 'find: <query>', or plain text queries without URL structure.
    Returns (resolved_url, is_search).
    """
    src = source.strip()
    src_lower = src.lower()
    if src_lower.startswith("search:"):
        query = src[7:].strip()
        return f"https://duckduckgo.com/html/?q={quote_plus(query)}", True
    if src_lower.startswith("find:"):
        query = src[5:].strip()
        return f"https://duckduckgo.com/html/?q={quote_plus(query)}", True
    if "://" not in src and "." not in src and " " in src:
        return f"https://duckduckgo.com/html/?q={quote_plus(src)}", True
    return src, False


class ResearchService:
    """Orchestrates end-to-end background research across multiple websites."""

    def __init__(self, task_manager=None):
        self.task_manager = task_manager
        self.research_tasks: Dict[str, ResearchTaskResponse] = {}
        self.comparison_store: Dict[str, ComparisonResult] = {}
        self.export_store: Dict[str, Dict[str, bytes]] = {}  # task_id -> {format: bytes}
        # Restore durable research status and comparison snapshots on process restart.
        try:
            for archived_task in task_archive.list_research_tasks():
                self.research_tasks[archived_task.task_id] = archived_task
                if archived_task.comparison is not None:
                    self.comparison_store[archived_task.task_id] = archived_task.comparison
        except Exception:
            logger.exception("Could not restore archived research tasks")

    async def submit_research_task(self, req: ResearchRequest) -> ResearchTaskResponse:
        """
        Validate research request, create background task, and start asynchronous execution.
        """
        if not req.goal or not req.goal.strip():
            raise ValueError("Research goal cannot be empty")
        if not req.sources:
            raise ValueError("At least one research source must be provided")

        from api.task_manager import TaskManager
        # Register in task_manager if available
        now = time.time()
        import uuid as _uuid
        task_id = f"research_{int(now)}_{_uuid.uuid4().hex[:8]}"

        # Initialize source statuses
        sources_status: List[SourceStatus] = []
        for src in req.sources:
            target_url, is_search = resolve_source_target(src)
            is_safe, reason, norm_url = validate_url_security(target_url, resolve_dns=False)
            if not is_safe:
                sources_status.append(SourceStatus(url=src, status="failed", error=f"Pre-validation rejected: {reason}"))
            else:
                sources_status.append(SourceStatus(url=src, status="pending"))

        response = ResearchTaskResponse(
            task_id=task_id,
            goal=req.goal,
            status=TaskStatus.PENDING,
            created_at=now,
            updated_at=now,
            sources=sources_status,
            reports={},
        )
        self.research_tasks[task_id] = response
        try:
            task_archive.save_research_task(response)
        except Exception:
            logger.exception("Could not archive new research task %s", task_id)

        # Also register in TaskManager tasks map so existing endpoints see it
        if self.task_manager:
            tm_task = TaskResponse(
                task_id=task_id,
                goal=req.goal,
                status=TaskStatus.PENDING,
                created_at=now,
                updated_at=now,
                initial_plan=[f"Research source {s}" for s in req.sources],
                plan=[f"Research source {s}" for s in req.sources],
                plan_status="pending",
                context=TaskContext(),
            )
            self.task_manager.tasks[task_id] = tm_task
            self.task_manager.cancel_events[task_id] = asyncio.Event()

        # Launch background execution task; retain reference to prevent GC
        bg_task = asyncio.create_task(self._run_research(task_id, req))
        if self.task_manager:
            self.task_manager.running_tasks[task_id] = bg_task
        return response

    async def _run_research(self, task_id: str, req: ResearchRequest) -> None:
        """Run research with a top-level failure boundary and guaranteed cleanup."""
        task_resp = self.research_tasks.get(task_id)
        try:
            await self._run_research_impl(task_id, req)
        except asyncio.CancelledError:
            logger.warning("Research worker cancelled for task %s", task_id)
            if task_resp is not None:
                task_resp.status = TaskStatus.CANCELLED
                task_resp.error = "Research task was cancelled."
                task_resp.updated_at = time.time()
                try:
                    task_archive.save_research_task(task_resp)
                except Exception:
                    logger.exception("Could not archive cancelled research task %s", task_id)
            if self.task_manager and task_id in self.task_manager.tasks:
                tm_task = self.task_manager.tasks[task_id]
                tm_task.status = TaskStatus.CANCELLED
                tm_task.error = "Research task was cancelled."
                tm_task.updated_at = time.time()
                try:
                    task_archive.save_task(tm_task, self.task_manager._report_store.get(task_id))
                except Exception:
                    logger.exception("Could not archive cancelled research task %s", task_id)
                try:
                    await self.task_manager.broadcast_task_update(task_id)
                except Exception:
                    logger.exception("Could not broadcast cancelled research task %s", task_id)
        except Exception as exc:
            logger.exception("Unhandled research worker failure for task %s", task_id)
            if task_resp is not None:
                task_resp.status = TaskStatus.FAILED
                task_resp.error = "Research failed unexpectedly. Check server logs for details."
                task_resp.updated_at = time.time()
                try:
                    task_archive.save_research_task(task_resp)
                except Exception:
                    logger.exception("Could not archive failed research task %s", task_id)
            if self.task_manager and task_id in self.task_manager.tasks:
                tm_task = self.task_manager.tasks[task_id]
                tm_task.status = TaskStatus.FAILED
                tm_task.error = "Research failed unexpectedly. Check server logs for details."
                tm_task.updated_at = time.time()
                try:
                    task_archive.save_task(tm_task, self.task_manager._report_store.get(task_id))
                except Exception:
                    logger.exception("Could not archive failed research task %s", task_id)
                try:
                    await self.task_manager.broadcast_task_update(task_id)
                except Exception:
                    logger.exception("Could not broadcast failed research task %s", task_id)
        finally:
            if self.task_manager:
                self.task_manager.tasks.pop(task_id, None)
                self.task_manager.running_tasks.pop(task_id, None)
                self.task_manager.cancel_events.pop(task_id, None)

    async def _run_research_impl(self, task_id: str, req: ResearchRequest) -> None:
        """Background execution worker across all sources."""
        task_resp = self.research_tasks[task_id]
        task_resp.status = TaskStatus.RUNNING
        task_resp.updated_at = time.time()
        try:
            task_archive.save_research_task(task_resp)
        except Exception:
            logger.exception("Could not archive running research task %s", task_id)

        if self.task_manager and task_id in self.task_manager.tasks:
            self.task_manager.tasks[task_id].status = TaskStatus.RUNNING
            await self.task_manager.broadcast_task_update(task_id)

        browser_pool = await get_browser_pool()
        concurrency_limit = min(req.concurrency, getattr(browser_pool, "max_concurrent_contexts", 8), 8)
        semaphore = asyncio.Semaphore(concurrency_limit)

        raw_records: List[Dict[str, Any]] = []
        sources_map = {s.url: s for s in task_resp.sources}

        async def process_single_source(source_url: str):
            status_item = sources_map.get(source_url)
            if not status_item:
                return

            if status_item.status == "failed":
                return

            # Check cancellation
            if self._is_task_cancelled(task_id):
                status_item.status = "skipped"
                status_item.error = "Task was cancelled before this source started"
                return

            target_url, is_search = resolve_source_target(source_url)

            # Re-verify URL security with DNS resolution
            is_safe, reason, clean_url = validate_url_security(target_url, resolve_dns=True)
            if not is_safe:
                status_item.status = "failed"
                status_item.error = f"Security check failed: {reason}"
                return

            nav_target = clean_url or target_url
            status_item.status = "running"
            if self.task_manager:
                await self.task_manager.broadcast_task_update(task_id)

            t0 = time.time()
            try:
                async with semaphore:
                    if self._is_task_cancelled(task_id):
                        status_item.status = "skipped"
                        return

                    async with browser_pool.acquire_context(viewport={"width": 1280, "height": 800}) as (ctx, page):
                        # Install SSRF / redirect navigation route guard
                        await install_navigation_guard(page)

                        nav_err = None
                        try:
                            await page.goto(nav_target, wait_until="domcontentloaded", timeout=req.timeout_per_source * 1000)
                            await settle_page(page, timeout=3000)
                        except Exception as exc:
                            logger.warning(f"Navigation warning for {nav_target}: {exc}")
                            nav_err = str(exc)

                        page_title = ""
                        try:
                            page_title = await page.title()
                        except Exception:
                            pass

                        status_item.title = page_title or source_url

                        # If navigation aborted before receiving any DOM or page title
                        if nav_err and not page_title:
                            status_item.status = "failed"
                            status_item.error = f"Navigation failed: {nav_err}"
                            status_item.duration_ms = (time.time() - t0) * 1000
                            return

                        extracted_source_records: List[Dict[str, Any]] = []

                        # Strategy 1: Multi-item listing extraction (catalog, search result cards, repeating items)
                        try:
                            listing_items = await extract_listing_records(page, req.criteria)
                            if listing_items and len(listing_items) > 0:
                                logger.info(f"Extracted {len(listing_items)} listing items from {nav_target}")
                                for item in listing_items:
                                    item["source_url"] = source_url
                                    extracted_source_records.append(item)
                        except Exception as list_exc:
                            logger.warning(f"Listing extraction error on {nav_target}: {list_exc}")

                        # Strategy 2: If no listing items, check if it's a search results page
                        if not extracted_source_records and (is_search or any(se in nav_target for se in ("duckduckgo.com", "google.", "bing.com", "yahoo.com"))):
                            try:
                                from extraction.search import extract_search_results
                                search_results = await extract_search_results(page)
                                if search_results:
                                    now_iso = datetime.now().isoformat()
                                    for sr in search_results:
                                        if sr.title:
                                            extracted_source_records.append({
                                                "title": sr.title,
                                                "name": sr.title,
                                                "product_url": sr.url or nav_target,
                                                "source_url": source_url,
                                                "description": sr.description,
                                                "domain": sr.domain,
                                                "extracted_at": now_iso,
                                                "evidence": {"title": f"Search result #{sr.position}: {sr.title}"},
                                            })
                            except Exception as s_exc:
                                logger.warning(f"Search results extraction error on {nav_target}: {s_exc}")

                        # Strategy 3: Single detail or general page fallback
                        if not extracted_source_records:
                            extractor = InfoExtractor()
                            extraction_res: ExtractionResult = await extractor.extract(page)
                            dom_criteria = await self._extract_criteria_from_page(page, req.criteria)

                            single_record = {
                                "source_url": source_url,
                                "product_url": page.url or nav_target,
                                "extracted_at": datetime.now().isoformat(),
                                "title": page_title or extraction_res.metadata.page_title,
                                "name": page_title or extraction_res.metadata.page_title,
                            }
                            if extraction_res.data and isinstance(extraction_res.data, dict):
                                single_record.update(extraction_res.data)
                            elif extraction_res.data and hasattr(extraction_res.data, "model_dump"):
                                single_record.update(extraction_res.data.model_dump())
                            single_record.update(dom_criteria)
                            extracted_source_records.append(single_record)

                        # Evaluate extraction success vs partial success
                        matching_criteria_count = 0
                        for rec in extracted_source_records:
                            for crit in (req.criteria or []):
                                if rec.get(crit) not in (None, "", "N/A"):
                                    matching_criteria_count += 1
                                    break

                        status_item.duration_ms = (time.time() - t0) * 1000
                        status_item.extracted_fields_count = len(extracted_source_records)

                        if not extracted_source_records:
                            status_item.status = "partial"
                            status_item.error = "Page loaded, but no data records could be extracted."
                        elif req.criteria and matching_criteria_count == 0:
                            status_item.status = "partial"
                            status_item.error = "Page loaded, but none of the requested comparison criteria could be extracted."
                            raw_records.extend(extracted_source_records)
                        else:
                            status_item.status = "completed"
                            raw_records.extend(extracted_source_records)

            except Exception as exc:
                status_item.status = "failed"
                status_item.error = str(exc)
                status_item.duration_ms = (time.time() - t0) * 1000
                logger.error(f"Error researching source {source_url}: {exc}")

        # Execute all sources with bounded concurrency
        source_tasks = [process_single_source(s.url) for s in task_resp.sources]
        await asyncio.gather(*source_tasks, return_exceptions=True)

        # Normalization and Deduplication
        criteria = req.criteria if req.criteria else ["price", "rating", "brand", "availability", "specs"]
        norm_records, conflicts = deduplicate_and_compare(raw_records, criteria)

        successful_sources = sum(1 for s in task_resp.sources if s.status == "completed")
        partial_sources = sum(1 for s in task_resp.sources if s.status == "partial")
        failed_sources = sum(1 for s in task_resp.sources if s.status == "failed")

        comparison = ComparisonResult(
            goal=req.goal,
            criteria=criteria,
            sources=task_resp.sources,
            records=norm_records,
            summary={
                "total_sources": len(task_resp.sources),
                "successful_sources": successful_sources,
                "partial_sources": partial_sources,
                "failed_sources": failed_sources,
                "total_records": len(norm_records),
                "conflicts_count": len(conflicts),
            },
        )
        self.comparison_store[task_id] = comparison
        task_resp.comparison = comparison

        # Generate Reports
        csv_bytes = ResearchExportService.export_csv(comparison)
        xlsx_bytes = ResearchExportService.export_xlsx(comparison)
        pdf_bytes = ResearchExportService.export_pdf(comparison)

        self.export_store[task_id] = {
            "csv": csv_bytes,
            "xlsx": xlsx_bytes,
            "pdf": pdf_bytes,
        }
        # Keep every format durable; the task archive's PDF column alone cannot
        # recover CSV/XLSX downloads after a backend restart.
        try:
            from services.task_archive import task_archive
            for export_format, export_bytes in self.export_store[task_id].items():
                task_archive.save_research_export(task_id, export_format, export_bytes)
        except Exception:
            logger.exception("Could not persist research exports for %s", task_id)

        task_resp.reports = {
            "csv": f"/api/research/{task_id}/export?format=csv",
            "xlsx": f"/api/research/{task_id}/export?format=xlsx",
            "pdf": f"/api/research/{task_id}/export?format=pdf",
        }

        # Determine terminal status
        if self._is_task_cancelled(task_id):
            task_resp.status = TaskStatus.CANCELLED
            task_resp.error = "Research task was cancelled by user"
        elif successful_sources == 0 and partial_sources == 0 and len(task_resp.sources) > 0:
            task_resp.status = TaskStatus.FAILED
            task_resp.error = "All research sources failed to load or extract."
        else:
            task_resp.status = TaskStatus.COMPLETED

        task_resp.updated_at = time.time()
        try:
            task_archive.save_research_task(task_resp)
        except Exception:
            logger.exception("Could not archive terminal research task %s", task_id)

        # Update TaskManager and task archive
        if self.task_manager:
            self.task_manager._report_store[task_id] = pdf_bytes
            if task_id in self.task_manager.tasks:
                tm_task = self.task_manager.tasks[task_id]
                tm_task.status = task_resp.status
                tm_task.reports = task_resp.reports
                tm_task.extracted_data = [r.model_dump() for r in norm_records]
                tm_task.error = task_resp.error
                tm_task.updated_at = task_resp.updated_at
                try:
                    task_archive.save_task(tm_task, pdf_bytes)
                except Exception as arc_exc:
                    logger.warning(f"Could not archive research task: {arc_exc}")
            await self.task_manager.broadcast_task_update(task_id)

            # Research tasks share the TaskManager registry with browser tasks. Once
            # the terminal snapshot is archived and broadcast, remove transient state
            # so completed research jobs do not accumulate in the active registry.
            self.task_manager.tasks.pop(task_id, None)
            self.task_manager.running_tasks.pop(task_id, None)
            self.task_manager.cancel_events.pop(task_id, None)

    async def _extract_criteria_from_page(self, page, criteria: List[str]) -> Dict[str, Any]:
        """Extract explicit criteria values and text evidence from the current DOM."""
        results: Dict[str, Any] = {}
        if not criteria:
            return results

        try:
            dom_data = await page.evaluate(
                """(criteriaList) => {
                    const extracted = {};
                    const bodyText = document.body ? document.body.innerText : "";

                    // Helper to check definition lists & table cells
                    const tables = Array.from(document.querySelectorAll('table'));
                    for (const table of tables) {
                        const rows = Array.from(table.querySelectorAll('tr'));
                        for (const row of rows) {
                            const cells = Array.from(row.querySelectorAll('th, td')).map(c => (c.innerText || '').trim());
                            if (cells.length >= 2) {
                                const key = cells[0].toLowerCase();
                                const val = cells[1];
                                for (const crit of criteriaList) {
                                    if (key.includes(crit.toLowerCase()) && !extracted[crit]) {
                                        extracted[crit] = val;
                                        extracted[crit + '_evidence'] = cells[0] + ': ' + val;
                                    }
                                }
                            }
                        }
                    }

                    // Helper to check dl / dt / dd
                    const dts = Array.from(document.querySelectorAll('dt'));
                    for (const dt of dts) {
                        const dd = dt.nextElementSibling;
                        if (dd && dd.tagName.toLowerCase() === 'dd') {
                            const key = (dt.innerText || '').toLowerCase();
                            const val = (dd.innerText || '').trim();
                            for (const crit of criteriaList) {
                                if (key.includes(crit.toLowerCase()) && !extracted[crit]) {
                                    extracted[crit] = val;
                                    extracted[crit + '_evidence'] = dt.innerText + ': ' + val;
                                }
                            }
                        }
                    }

                    // Helper to check meta tags
                    for (const crit of criteriaList) {
                        if (!extracted[crit]) {
                            const meta = document.querySelector(`meta[name*="${crit}" i], meta[property*="${crit}" i]`);
                            if (meta && meta.content) {
                                extracted[crit] = meta.content;
                                extracted[crit + '_evidence'] = 'meta tag: ' + meta.content;
                            }
                        }
                    }

                    return extracted;
                }""",
                criteria,
            )
            if isinstance(dom_data, dict):
                results.update(dom_data)
        except Exception as exc:
            logger.debug(f"DOM criteria evaluation error: {exc}")

        return results

    def _is_task_cancelled(self, task_id: str) -> bool:
        if not self.task_manager:
            return False
        return self.task_manager._is_cancelled(task_id)

    def cancel_research_task(self, task_id: str) -> Optional[ResearchTaskResponse]:
        """Signal cancellation for a running research task."""
        if task_id in self.research_tasks:
            t = self.research_tasks[task_id]
            if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                t.status = TaskStatus.CANCELLED
                t.updated_at = time.time()
                t.error = "Research task was cancelled by user"
                if self.task_manager and task_id in self.task_manager.cancel_events:
                    self.task_manager.cancel_events[task_id].set()
                try:
                    task_archive.save_research_task(t)
                except Exception:
                    logger.exception("Could not archive research cancellation %s", task_id)
                if self.task_manager and task_id in self.task_manager.tasks:
                    tm_task = self.task_manager.tasks[task_id]
                    tm_task.status = TaskStatus.CANCELLED
                    tm_task.error = "Task cancelled by user"
                    tm_task.updated_at = t.updated_at
                return t
        return None

    def get_research_task(self, task_id: str) -> Optional[ResearchTaskResponse]:
        task = self.research_tasks.get(task_id)
        if task is not None:
            return task
        # Fall back to durable storage in case the in-memory cache was cleared.
        try:
            task = task_archive.get_research_task(task_id)
        except Exception:
            logger.exception("Could not load archived research task %s", task_id)
            return None
        if task is not None:
            self.research_tasks[task_id] = task
            if task.comparison is not None:
                self.comparison_store[task_id] = task.comparison
        return task

    def get_results(self, task_id: str) -> Optional[ComparisonResult]:
        result = self.comparison_store.get(task_id)
        if result is not None:
            return result
        task = self.get_research_task(task_id)
        if task and task.comparison is not None:
            self.comparison_store[task_id] = task.comparison
            return task.comparison
        return None

    def get_export_report(self, task_id: str, fmt: str = "pdf") -> Optional[bytes]:
        normalized_format = (fmt or "pdf").lower()
        task_exports = self.export_store.get(task_id)
        if task_exports and task_exports.get(normalized_format):
            return task_exports[normalized_format]
        try:
            from services.task_archive import task_archive
            return task_archive.get_research_export(task_id, normalized_format)
        except Exception:
            logger.exception("Could not load research export %s for %s", normalized_format, task_id)
            return None


# Global singleton
research_service = ResearchService()
