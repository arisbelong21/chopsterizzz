"""Chopster Automation — local-first publishing/authorization/scheduling engine.

Public entry points:
    chopster.automation.manager.AutomationManager  (created by Application)
    chopster.automation.events                     (ACS/CCA completion bridge)
    chopster.automation.autopilot.AutoPilotEngine  (event-driven rules)
    chopster.automation.planner                    (staggered/rule scheduling)
"""
from __future__ import annotations

__all__ = ["autopilot", "content", "events", "models", "planner", "queue", "scheduler"]
