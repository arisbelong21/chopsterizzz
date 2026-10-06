"""AI provider interface — adapter for transcript analysis, caption generation, visual."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class AIRequest:
    prompt: str
    system: str = ""
    model: str = ""
    temperature: float = 0.7
    max_tokens: int | None = None
    timeout: int = 60


@dataclass
class AIResponse:
    text: str
    raw: Any = None
    usage: dict | None = None


class AIProvider(ABC):
    @abstractmethod
    def generate(self, req: AIRequest) -> AIResponse:
        ...

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """Return (available, message)."""
        ...

    def generate_vision(self, prompt: str, image_paths: list[str], *, system: str = "", model: str = "", temperature: float = 0.2, max_tokens: int = 4096, timeout: int | None = None) -> AIResponse:
        """Optional multimodal capability. Providers without native vision may raise."""
        raise NotImplementedError(f"Provider {self.name} tidak menyediakan vision")

    @property
    @abstractmethod
    def name(self) -> str:
        ...
