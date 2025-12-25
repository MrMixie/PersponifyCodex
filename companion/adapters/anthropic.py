from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import List, Optional

from .base import BaseAdapter


class AnthropicAdapter(BaseAdapter):
    @property
    def type(self) -> str:
        return "anthropic"

    def is_available(self) -> bool:
        return bool(self._api_key())

    def capabilities(self) -> dict:
        return {
            "streaming": False,
            "system_prompt": True,
            "tool_calls": False,
            "resource": "remote",
        }

    # Non-streaming keeps the implementation simple and reliable.
    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        api_key = self._api_key()
        if not api_key:
            raise RuntimeError("Anthropic API key missing")
        url = f"{self._base_url()}/messages"
        payload = {
            "model": self._model(),
            "max_tokens": int(self.settings.get("max_tokens") or 1024),
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "x-api-key": api_key,
            "anthropic-version": str(self.settings.get("version") or "2023-06-01"),
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=self._timeout()) as resp:
                raw = resp.read().decode("utf-8")
            return self._extract_text(json.loads(raw))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8") if exc.fp else str(exc)
            raise RuntimeError(f"Anthropic request failed: {exc.code} {detail}") from exc

    def list_models(self) -> List[dict]:
        api_key = self._api_key()
        if not api_key:
            raise RuntimeError("Anthropic API key missing")
        url = f"{self._base_url()}/models"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": str(self.settings.get("version") or "2023-06-01"),
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=self._timeout()) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8") if exc.fp else str(exc)
            raise RuntimeError(f"Anthropic request failed: {exc.code} {detail}") from exc
        models = data.get("data") or data.get("models") or []
        out = []
        for item in models:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name")
                if model_id:
                    out.append({"id": model_id})
        return out

    def _api_key(self) -> Optional[str]:
        return self.settings.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")

    def _base_url(self) -> str:
        return str(self.settings.get("base_url") or "https://api.anthropic.com/v1").rstrip("/")

    def _model(self) -> str:
        return str(self.settings.get("model") or "claude-3-5-sonnet-20240620")

    def _timeout(self) -> float:
        return float(self.settings.get("timeout") or 60)

    def resource_hint(self) -> str:
        return "remote"

    @staticmethod
    def _extract_text(data: dict) -> str:
        blocks = data.get("content") or []
        parts = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
