from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class AdapterConfig:
    name: str
    type: str
    enabled: bool = True
    settings: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AppConfig:
    version: str = "1"
    default_adapter: Optional[str] = None
    adapters: List[AdapterConfig] = field(default_factory=list)


def _parse_adapter(raw: Dict[str, Any]) -> AdapterConfig:
    return AdapterConfig(
        name=str(raw.get("name", "")),
        type=str(raw.get("type", "")),
        enabled=bool(raw.get("enabled", True)),
        settings=dict(raw.get("settings", {})),
    )


# Keep parsing straightforward so configs stay human-editable.
def load_config(path: str | Path) -> AppConfig:
    data = json.loads(Path(path).read_text())
    adapters = [_parse_adapter(a) for a in data.get("adapters", [])]
    return AppConfig(
        version=str(data.get("version", "1")),
        default_adapter=data.get("default_adapter"),
        adapters=adapters,
    )


def select_adapter(config: AppConfig, name: Optional[str]) -> Optional[AdapterConfig]:
    if name:
        for a in config.adapters:
            if a.name == name:
                return a
    if config.default_adapter:
        for a in config.adapters:
            if a.name == config.default_adapter:
                return a
    for a in config.adapters:
        if a.enabled:
            return a
    return None
