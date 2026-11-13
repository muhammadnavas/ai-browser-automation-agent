"""Read-only interaction reconnaissance specialist.

This worker identifies actionable controls for the master agent. It never
clicks, fills, submits, or otherwise changes browser state.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from agents.subagents import SubagentContext, subagent_orchestrator
from agents.browser_subagents import _open_owned_tab, _close_owned_tab


async def interaction_worker(ctx: SubagentContext) -> Dict[str, Any]:
    """Inspect visible controls and return bounded, non-mutating evidence."""
    from agents.browser_subagents import _observation_adapter, _inspect_observation
    adapter, owns_adapter = await _observation_adapter(ctx)
    try:
        ctx.raise_if_cancelled()
        page = await _inspect_observation(ctx, adapter)
        if page.get("inspection_error") or not page.get("url"):
            raise RuntimeError("Interaction specialist could not inspect the live page")

        controls: List[Dict[str, Any]] = []
        for item in (page.get("elements") or []):
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag") or "").lower()
            role = str(item.get("role") or "").lower()
            if tag not in {"a", "button", "input", "select", "textarea"} and role not in {
                "button", "link", "menuitem", "tab", "checkbox", "radio", "combobox"
            }:
                continue
            controls.append({
                "index": item.get("index"),
                "tag": tag[:30],
                "role": role[:40],
                "text": str(item.get("text") or "")[:180],
                "label": str(item.get("label") or item.get("aria_label") or "")[:180],
                "placeholder": str(item.get("placeholder") or "")[:120],
                "href": str(item.get("href") or "")[:600],
                "disabled": bool(item.get("disabled", False)),
            })
            if len(controls) >= 80:
                break

        return {
            "url": str(page.get("url") or "")[:1000],
            "title": str(page.get("title") or "")[:240],
            "controls": controls,
            "control_count": len(controls),
            "read_only": True,
            "note": "Controls are observations only; the master executor must re-inspect before acting.",
        }
    finally:
        if owns_adapter:
            await _close_owned_tab(adapter)


def register_interaction_worker() -> None:
    subagent_orchestrator.register_worker("interaction", interaction_worker)
