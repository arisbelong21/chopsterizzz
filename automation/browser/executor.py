"""Deterministic executor: validated structured action → Playwright call.

Semantic targeting order (spec §20): accessibility role/name → visible
label/text → stable attributes → CSS selector. Coordinates are rejected by
the validator, so they can never occur here.
"""
from __future__ import annotations

import time
from typing import Any


class ExecutorError(RuntimeError):
    pass


def _target_to_locator(page: Any, target: Any) -> Any:
    if isinstance(target, dict):
        role = target.get("role")
        name = target.get("name") or target.get("accessible_name")
        text = target.get("text") or target.get("label")
        placeholder = target.get("placeholder")
        selector = target.get("selector")
        if role and name:
            try:
                return page.get_by_role(role, name=name)
            except Exception:
                pass
        if text:
            try:
                loc = page.get_by_text(str(text), exact=False)
                if loc.count() > 0:
                    return loc.first
            except Exception:
                pass
            try:
                return page.get_by_label(str(text))
            except Exception:
                pass
        if placeholder:
            try:
                return page.get_by_placeholder(str(placeholder))
            except Exception:
                pass
        if selector:
            return page.locator(str(selector))
        raise ExecutorError(f"target cannot be resolved: {target}")
    if isinstance(target, str):
        try:
            loc = page.get_by_text(target, exact=False)
            if loc.count() > 0:
                return loc.first
        except Exception:
            pass
        return page.locator(target)
    raise ExecutorError("target must be object or string")


def _resolve_one(page: Any, target: Any) -> Any:
    loc = _target_to_locator(page, target)
    try:
        loc.first.wait_for(state="visible", timeout=8000)
    except Exception as exc:
        raise ExecutorError(f"target not visible: {exc}") from exc
    return loc.first


def execute(page: Any, action: dict[str, Any]) -> str:
    """Execute one validated action; returns a short outcome string."""
    atype = str(action.get("type") or "").upper()
    target = action.get("target")
    value = action.get("value")
    if atype == "CLICK":
        _resolve_one(page, target).click(timeout=8000)
        return "clicked"
    if atype == "FOCUS":
        _resolve_one(page, target).focus(timeout=8000)
        return "focused"
    if atype == "TYPE":
        loc = _resolve_one(page, target)
        loc.click(timeout=8000)
        loc.fill(str(value), timeout=8000)
        return "typed"
    if atype == "SELECT":
        _resolve_one(page, target).select_option(str(value), timeout=8000)
        return "selected"
    if atype == "UPLOAD_FILE":
        inputs = page.locator("input[type='file']")
        inputs.first.wait_for(state="attached", timeout=8000)
        inputs.first.set_input_files(str(value))
        return "file_set"
    if atype == "SCROLL":
        direction = str(value or "down").lower()
        delta = 600 if direction == "down" else -600
        page.mouse.wheel(0, delta)
        return "scrolled"
    if atype == "WAIT":
        try:
            secs = float(value or 1)
        except Exception:
            secs = 1
        time.sleep(max(0.0, min(20.0, secs)))
        return "waited"
    if atype == "NAVIGATE":
        page.goto(str(value), wait_until="domcontentloaded")
        return "navigated"
    if atype == "PRESS_KEY":
        page.keyboard.press(str(value or "Enter"))
        return "key_pressed"
    if atype == "READ":
        return "read"
    if atype == "VERIFY":
        try:
            loc = page.get_by_text(str(value), exact=False)
            found = loc.count() > 0
        except Exception:
            found = False
        if not found:
            raise ExecutorError(f"verification text not found: {value}")
        return "verified"
    if atype in ("REQUEST_USER_ACTION", "STOP"):
        return atype.lower()
    raise ExecutorError(f"unhandled action type: {atype}")
