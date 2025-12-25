from __future__ import annotations

from typing import Dict, Iterable, Optional

from .config import AppConfig, AdapterConfig, load_config, select_adapter
from .adapters.base import BaseAdapter
from .adapters.anthropic import AnthropicAdapter
from .adapters.echo import EchoAdapter
from .adapters.ollama import OllamaAdapter
from .adapters.openai import OpenAIAdapter
from .adapters.xai import XAIAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self._factories = {
            "echo": EchoAdapter,
            "openai": OpenAIAdapter,
            "anthropic": AnthropicAdapter,
            "xai": XAIAdapter,
            "ollama": OllamaAdapter,
        }

    def create(self, cfg: AdapterConfig, settings_override: Optional[Dict] = None) -> BaseAdapter:
        adapter_cls = self._factories.get(cfg.type)
        if not adapter_cls:
            raise ValueError(f"Unknown adapter type: {cfg.type}")
        settings = settings_override if settings_override is not None else cfg.settings
        return adapter_cls(cfg.name, settings)


class HeadlessService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.registry = AdapterRegistry()
        # Cache adapters so we don't re-auth on every request.
        self.adapters: Dict[str, BaseAdapter] = {}
        self._secrets_by_adapter: Dict[str, Dict[str, str]] = {}
        self._secrets_by_type: Dict[str, Dict[str, str]] = {}
        for a in config.adapters:
            if not a.enabled:
                continue
            self.adapters[a.name] = self._build_adapter(a)

    @classmethod
    def from_path(cls, path: str) -> "HeadlessService":
        return cls(load_config(path))

    def resolve_adapter(self, name: Optional[str]) -> BaseAdapter:
        cfg = select_adapter(self.config, name)
        if not cfg:
            raise RuntimeError("No enabled adapters in config")
        adapter = self.adapters.get(cfg.name)
        if not adapter:
            adapter = self._build_adapter(cfg)
            self.adapters[cfg.name] = adapter
        return adapter

    def apply_secrets(
        self,
        by_adapter: Optional[Dict[str, Dict[str, str]]] = None,
        by_type: Optional[Dict[str, Dict[str, str]]] = None,
        replace: bool = False,
    ) -> None:
        if replace:
            self._secrets_by_adapter = {}
            self._secrets_by_type = {}

        if by_adapter:
            for key, payload in by_adapter.items():
                if payload:
                    self._secrets_by_adapter[key] = dict(payload)
        if by_type:
            for key, payload in by_type.items():
                if payload:
                    self._secrets_by_type[key] = dict(payload)

        if by_adapter:
            for name in by_adapter.keys():
                self.adapters.pop(name, None)
        if by_type:
            for adapter_type in by_type.keys():
                for cfg in self.config.adapters:
                    if cfg.type == adapter_type:
                        self.adapters.pop(cfg.name, None)

    def complete(self, prompt: str, system: Optional[str] = None, adapter_name: Optional[str] = None) -> str:
        adapter = self.resolve_adapter(adapter_name)
        return adapter.complete(prompt, system=system)

    def stream(self, prompt: str, system: Optional[str] = None, adapter_name: Optional[str] = None) -> Iterable[str]:
        adapter = self.resolve_adapter(adapter_name)
        return adapter.stream(prompt, system=system)

    def _build_adapter(self, cfg: AdapterConfig) -> BaseAdapter:
        settings = dict(cfg.settings or {})
        type_secret = self._secrets_by_type.get(cfg.type)
        if type_secret:
            settings.update(type_secret)
        adapter_secret = self._secrets_by_adapter.get(cfg.name)
        if adapter_secret:
            settings.update(adapter_secret)
        return self.registry.create(cfg, settings_override=settings)
