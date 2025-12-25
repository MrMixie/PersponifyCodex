from __future__ import annotations

from .base import BaseAdapter


class EchoAdapter(BaseAdapter):
    @property
    def type(self) -> str:
        return "echo"

    # Handy for debugging the request pipeline without external calls.
    def complete(self, prompt: str, system: str | None = None) -> str:
        prefix = str(self.settings.get("prefix", ""))
        if system:
            return f"{prefix}{system}\n{prompt}"
        return f"{prefix}{prompt}"

    def resource_hint(self) -> str:
        return "local"
