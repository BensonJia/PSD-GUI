#!/usr/bin/env python3
"""Build the viewer's static equation catalogue from the vendored PSID source."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


FUNCTION_START = re.compile(r"^function\s+([A-Za-z_][A-Za-z0-9_!]*)", re.MULTILINE)
TYPE_REFERENCE = re.compile(r"PSY\.([A-Z][A-Za-z0-9_]*)")
ASSIGNMENT = re.compile(
    r"^\s*((?:[A-Za-z_]+_ode|inner_vars|mass_matrix|current_[ri](?:_from|_to)?)\[.+\])\s*([+\-]?=)\s*(.*)$"
)
LIMITER_RELATION = re.compile(
    r"^\s*((?:Id|Iq)_cnv_ref2|Del_Vv_[dq])\s*=\s*(.*)$"
)

ABSTRACT_TYPES = {
    "AVR",
    "Converter",
    "DCSource",
    "DynamicGenerator",
    "DynamicInverter",
    "Filter",
    "FrequencyEstimator",
    "InnerControl",
    "Machine",
    "OuterControl",
    "OutputCurrentLimiter",
    "PSS",
    "Shaft",
    "TurbineGov",
}

SPECIAL_TYPES = {
    "mdl_branch_ode!": ("DynamicBranch",),
    "mdl_transformer_Lshape_ode!": ("DynamicTransformer",),
    "mdl_zip_load!": ("StandardLoad",),
}

CATEGORY_NAMES = {
    "avr_models.jl": "励磁系统",
    "converter_models.jl": "变流器",
    "DCside_models.jl": "直流侧",
    "dynline_model.jl": "动态支路",
    "filter_models.jl": "滤波器",
    "frequency_estimator_models.jl": "频率估计器",
    "inner_control_models.jl": "内环控制器",
    "load_models.jl": "负荷",
    "machine_models.jl": "同步机",
    "outer_control_models.jl": "外环控制器",
    "output_current_limiter_models.jl": "输出电流限幅器",
    "pss_models.jl": "电力系统稳定器",
    "shaft_models.jl": "轴系",
    "source_models.jl": "电源",
    "tg_models.jl": "调速器",
    "device.jl": "聚合动态设备",
}


def _signature(block: str) -> str:
    lines = block.splitlines()
    depth = 0
    seen = False
    result: list[str] = []
    for line in lines:
        result.append(line.strip())
        depth += line.count("(") - line.count(")")
        seen = seen or "(" in line
        if seen and depth <= 0:
            break
    return " ".join(result)


def _assignments(block: str) -> list[str]:
    lines = block.splitlines()
    result: list[str] = []
    index = 0
    while index < len(lines):
        match = ASSIGNMENT.match(lines[index])
        if not match:
            index += 1
            continue
        expression = match.group(3).split("#", 1)[0].strip()
        parts = [expression] if expression else []
        depth = expression.count("(") + expression.count("[") - expression.count(")") - expression.count("]")
        while index + 1 < len(lines) and (not parts or depth > 0):
            index += 1
            continuation = lines[index].split("#", 1)[0].strip()
            if continuation:
                parts.append(continuation)
                depth += continuation.count("(") + continuation.count("[") - continuation.count(")") - continuation.count("]")
        rhs = " ".join(parts)
        if rhs:
            result.append(f"{match.group(1)} {match.group(2)} {rhs}")
        index += 1
    return result


def _limiter_relations(block: str) -> list[str]:
    result = []
    for line in block.splitlines():
        match = LIMITER_RELATION.match(line)
        if match:
            result.append(
                f"{match.group(1)} = {match.group(2).split('#', 1)[0].strip()}"
            )
    return result


def build(source_root: Path) -> dict[str, object]:
    model_root = source_root / "src" / "models"
    models: dict[str, list[dict[str, object]]] = {}
    mass_matrices: dict[str, list[str]] = {}
    for path in sorted(model_root.rglob("*.jl")):
        if path.name not in CATEGORY_NAMES:
            continue
        text = path.read_text(encoding="utf-8")
        starts = list(FUNCTION_START.finditer(text))
        for number, start in enumerate(starts):
            end = starts[number + 1].start() if number + 1 < len(starts) else len(text)
            block = text[start.start() : end]
            entrypoint = start.group(1)
            signature = _signature(block)
            types = {
                model_type
                for model_type in TYPE_REFERENCE.findall(signature)
                if model_type not in ABSTRACT_TYPES
            }
            types.update(SPECIAL_TYPES.get(entrypoint, ()))
            equations = _assignments(block)
            if "mass_matrix" in entrypoint:
                matrix_equations = [
                    item for item in equations if item.startswith("mass_matrix[")
                ]
                for model_type in types:
                    mass_matrices.setdefault(model_type, []).extend(matrix_equations)
                continue
            accepted = entrypoint.startswith(("mdl_", "_mdl_")) or entrypoint == "device!"
            if entrypoint == "limit_output_current":
                equations.extend(_limiter_relations(block))
                accepted = True
            if not accepted:
                continue
            if not types or not equations:
                continue
            derivative = [item for item in equations if "_ode[" in item]
            algebraic = [
                item
                for item in equations
                if "_ode[" not in item and not item.startswith("mass_matrix[")
            ]
            implementation = {
                "category": CATEGORY_NAMES[path.name],
                "entrypoint": entrypoint,
                "source": path.relative_to(source_root).as_posix(),
                "derivatives": derivative,
                "algebraic": algebraic,
                "mass_matrix": [],
            }
            for model_type in sorted(types):
                bucket = models.setdefault(model_type, [])
                if implementation not in bucket:
                    bucket.append(implementation)
    for model_type, implementations in models.items():
        matrix = list(dict.fromkeys(mass_matrices.get(model_type, ())))
        for implementation in implementations:
            implementation["mass_matrix"] = matrix
    return {
        "schema_version": 1,
        "source": "PowerSimulationsDynamics.jl/src/models",
        "description": "由当前 PowerSimulationsDynamics.jl 源码中的 mdl_* 实现生成。",
        "models": dict(sorted(models.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("PowerSimulationsDynamics.jl"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("psid_graph_viewer/model_library.json"),
    )
    args = parser.parse_args()
    catalogue = build(args.source.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(catalogue['models'])} model types to {args.output}")


if __name__ == "__main__":
    main()
