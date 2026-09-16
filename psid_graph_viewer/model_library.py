from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any


@dataclass(frozen=True)
class ModelImplementation:
    model_type: str
    category: str
    entrypoint: str
    source: str
    derivatives: tuple[str, ...]
    algebraic: tuple[str, ...]
    mass_matrix: tuple[str, ...]


class ModelLibrary:
    """Read-only access to equations extracted from the bundled PSID source."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        if data is None:
            resource = files("psid_graph_viewer").joinpath("model_library.json")
            data = json.loads(resource.read_text(encoding="utf-8"))
        self._models = data.get("models", {})

    @property
    def model_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._models))

    def lookup(self, model_type: str) -> tuple[ModelImplementation, ...]:
        candidates = [model_type]
        candidates.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", model_type))
        result: list[ModelImplementation] = []
        seen: set[tuple[str, str, str]] = set()
        for candidate in candidates:
            for raw in self._models.get(candidate, []):
                key = (candidate, raw["entrypoint"], raw["source"])
                if key in seen:
                    continue
                seen.add(key)
                result.append(
                    ModelImplementation(
                        model_type=candidate,
                        category=raw["category"],
                        entrypoint=raw["entrypoint"],
                        source=raw["source"],
                        derivatives=tuple(raw.get("derivatives", ())),
                        algebraic=tuple(raw.get("algebraic", ())),
                        mass_matrix=tuple(raw.get("mass_matrix", ())),
                    )
                )
        return tuple(result)
