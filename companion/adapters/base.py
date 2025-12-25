from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Iterable, List, Optional


class BaseAdapter(ABC):
    """Adapter interface for local or remote AI backends."""

    def __init__(self, name: str, settings: Optional[Dict] = None) -> None:
        self.name = name
        self.settings = settings or {}

    @property
    @abstractmethod
    def type(self) -> str:
        raise NotImplementedError

    def is_available(self) -> bool:
        return True

    def capabilities(self) -> Dict:
        # Keep defaults optimistic; adapters can override as needed.
        return {
            "streaming": True,
            "system_prompt": True,
            "tool_calls": False,
            "resource": self.resource_hint(),
        }

    def resource_hint(self) -> str:
        return "remote"

    @abstractmethod
    def complete(self, prompt: str, system: str | None = None) -> str:
        raise NotImplementedError

    def stream(self, prompt: str, system: str | None = None) -> Iterable[str]:
        """Default streaming: yield the full completion once."""
        yield self.complete(prompt, system=system)

    def list_models(self) -> List[Dict]:
        raise NotImplementedError("Model listing not supported for this adapter.")
