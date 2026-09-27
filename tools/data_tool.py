from __future__ import annotations

import csv
import json
import os
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import Settings
from core.logging_setup import get_logger
from tools.registry import ToolResult

log = get_logger("data_tool")

_MAX_ROWS_PREVIEW = 50
_MAX_FILE_SIZE = 50 * 1024 * 1024


def _resolve_path(path: str, settings: Settings) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = (settings.base_dir / p).resolve()
    return p


def _check_allowed(path: Path, settings: Settings) -> bool:
    try:
        return any(path.is_relative_to(root) for root in settings.allowed_roots)
    except Exception:
        return False


def load_csv(path: str, settings: Settings) -> ToolResult:
    """Load a CSV file and return basic information about its structure."""
    try:
        p = _resolve_path(path, settings)
        if not p.is_file():
            return ToolResult(False, error=f"Not a file: {p}")
        if p.stat().st_size > _MAX_FILE_SIZE:
            return ToolResult(False, error=f"File too large (>50MB): {p}")
        if not _check_allowed(p, settings):
            return ToolResult(False, error="Path outside allowed roots")

        with p.open("r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            rows = list(reader)

        if not rows:
            return ToolResult(True, output="(empty CSV)", data={"row_count": 0, "columns": []})

        headers = rows[0]
        data_rows = rows[1:]

        # Detect column types from first few data rows
        col_types = []
        for i, header in enumerate(headers):
            values = [row[i] for row in data_rows[:10] if i < len(row)]
            col_type = _infer_type(values)
            col_types.append({"name": header, "type": col_type, "sample": values[:3]})

        preview = [headers] + data_rows[:5]
        output_lines = [f"CSV: {p.name}", f"Rows: {len(data_rows)} (header + {len(data_rows)} data)"]
        output_lines.append(f"Columns ({len(headers)}): {', '.join(headers)}")
        output_lines.append("")
        output_lines.append("Column types:")
        for ct in col_types:
            output_lines.append(f"  {ct['name']}: {ct['type']} (samples: {ct['sample']})")
        output_lines.append("")
        output_lines.append("Preview (first 5 rows):")
        for row in preview:
            output_lines.append("  " + " | ".join(row[:10]))

        return ToolResult(
            True,
            output="\n".join(output_lines),
            data={
                "path": str(p),
                "row_count": len(data_rows),
                "headers": headers,
                "column_types": col_types,
                "preview": preview,
            },
        )
    except Exception as exc:
        return ToolResult(False, error=f"Failed to load CSV: {exc}")


def load_json(path: str, settings: Settings) -> ToolResult:
    """Load a JSON file and return its structure."""
    try:
        p = _resolve_path(path, settings)
        if not p.is_file():
            return ToolResult(False, error=f"Not a file: {p}")
        if p.stat().st_size > _MAX_FILE_SIZE:
            return ToolResult(False, error=f"File too large (>50MB): {p}")
        if not _check_allowed(p, settings):
            return ToolResult(False, error="Path outside allowed roots")

        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)

        info = _describe_json(data)
        return ToolResult(
            True,
            output=f"JSON: {p.name}\n{info}",
            data={"path": str(p), "structure": data if isinstance(data, dict) else {"type": type(data).__name__, "length": len(data) if hasattr(data, "__len__") else 0}},
        )
    except Exception as exc:
        return ToolResult(False, error=f"Failed to load JSON: {exc}")


def _describe_json(obj: Any, depth: int = 0, max_depth: int = 3) -> str:
    indent = "  " * depth
    if depth >= max_depth:
        return f"{indent}... (max depth reached)"
    if isinstance(obj, dict):
        lines = [f"{indent}Object with {len(obj)} keys:"]
        for k, v in list(obj.items())[:10]:
            lines.append(f"{indent}  {k}: {_describe_json(v, depth + 1, max_depth).strip()}")
        if len(obj) > 10:
            lines.append(f"{indent}  ... and {len(obj) - 10} more keys")
        return "\n".join(lines)
    elif isinstance(obj, list):
        if not obj:
            return f"{indent}Empty array"
        sample_type = type(obj[0]).__name__
        lines = [f"{indent}Array[{sample_type}] with {len(obj)} items:"]
        if isinstance(obj[0], (dict, list)):
            lines.append(_describe_json(obj[0], depth + 1, max_depth))
        else:
            lines.append(f"{indent}  sample: {obj[:5]}")
        return "\n".join(lines)
    else:
        return f"{indent}{type(obj).__name__}: {obj}"


def _infer_type(values: list[str]) -> str:
    if not values:
        return "unknown"
    non_empty = [v for v in values if v and v.strip()]
    if not non_empty:
        return "empty"
    # Try int
    try:
        for v in non_empty:
            int(v)
        return "integer"
    except ValueError:
        pass
    # Try float
    try:
        for v in non_empty:
            float(v)
        return "float"
    except ValueError:
        pass
    # Check boolean
    if all(v.lower() in ("true", "false", "1", "0", "yes", "no") for v in non_empty):
        return "boolean"
    return "string"


def inspect_data(path: str, settings: Settings) -> ToolResult:
    """Inspect a data file (CSV or JSON) and return summary statistics."""
    p = _resolve_path(path, settings)
    if not p.is_file():
        return ToolResult(False, error=f"Not a file: {p}")
    if not _check_allowed(p, settings):
        return ToolResult(False, error="Path outside allowed roots")

    if p.suffix.lower() == ".csv":
        return _inspect_csv(p)
    elif p.suffix.lower() in (".json",):
        return _inspect_json(p)
    else:
        return ToolResult(False, error=f"Unsupported file type: {p.suffix}. Supported: .csv, .json")


def _inspect_csv(path: Path) -> ToolResult:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            rows = list(reader)

        if not rows:
            return ToolResult(True, output="(empty CSV)", data={"row_count": 0, "columns": []})

        headers = rows[0]
        data_rows = rows[1:]

        col_stats = []
        for i, header in enumerate(headers):
            values = [row[i] for row in data_rows if i < len(row) and row[i] and row[i].strip()]
            stats = _compute_column_stats(values)
            stats["name"] = header
            stats["index"] = i
            col_stats.append(stats)

        missing_counts = []
        for i, header in enumerate(headers):
            missing = sum(1 for row in data_rows if i >= len(row) or not row[i] or not row[i].strip())
            missing_counts.append({"column": header, "missing": missing, "missing_pct": round(missing / len(data_rows) * 100, 1) if data_rows else 0})

        output = [
            f"CSV Inspection: {path.name}",
            f"Rows: {len(data_rows)} data rows + 1 header",
            f"Columns: {len(headers)}",
            "",
            "Column Statistics:",
        ]
        for cs in col_stats:
            output.append(f"  {cs['name']} ({cs['type']}): count={cs['count']}, unique={cs['unique']}")
            if cs['type'] in ('integer', 'float') and 'min' in cs:
                output.append(f"    min={cs['min']}, max={cs['max']}, mean={cs['mean']:.2f}, median={cs['median']:.2f}, std={cs['std']:.2f}")

        output.append("")
        output.append("Missing Values:")
        for mc in missing_counts:
            if mc['missing'] > 0:
                output.append(f"  {mc['column']}: {mc['missing']} ({mc['missing_pct']}%)")

        return ToolResult(
            True,
            output="\n".join(output),
            data={
                "path": str(path),
                "row_count": len(data_rows),
                "headers": headers,
                "column_stats": col_stats,
                "missing_values": missing_counts,
            },
        )
    except Exception as exc:
        return ToolResult(False, error=f"Failed to inspect CSV: {exc}")


def _inspect_json(path: Path) -> ToolResult:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        info = _describe_json(data)
        return ToolResult(
            True,
            output=f"JSON Inspection: {path.name}\n{info}",
            data={"path": str(path), "structure_type": type(data).__name__},
        )
    except Exception as exc:
        return ToolResult(False, error=f"Failed to inspect JSON: {exc}")


def _compute_column_stats(values: list[str]) -> dict[str, Any]:
    non_empty = [v for v in values if v and v.strip()]
    if not non_empty:
        return {"count": 0, "unique": 0, "type": "empty"}

    # Try numeric
    numeric_vals = []
    for v in non_empty:
        try:
            numeric_vals.append(float(v))
        except ValueError:
            numeric_vals = None
            break

    if numeric_vals is not None and numeric_vals:
        return {
            "count": len(numeric_vals),
            "unique": len(set(numeric_vals)),
            "type": "float" if any("." in str(v) for v in non_empty) else "integer",
            "min": min(numeric_vals),
            "max": max(numeric_vals),
            "mean": statistics.mean(numeric_vals),
            "median": statistics.median(numeric_vals),
            "std": statistics.stdev(numeric_vals) if len(numeric_vals) > 1 else 0,
        }

    # Check boolean
    bool_vals = [v.lower() for v in non_empty]
    if all(v in ("true", "false", "1", "0", "yes", "no") for v in bool_vals):
        true_count = sum(1 for v in bool_vals if v in ("true", "1", "yes"))
        return {
            "count": len(non_empty),
            "unique": len(set(non_empty)),
            "type": "boolean",
            "true_count": true_count,
            "false_count": len(non_empty) - true_count,
        }

    return {
        "count": len(non_empty),
        "unique": len(set(non_empty)),
        "type": "string",
        "top_values": Counter(non_empty).most_common(5),
    }


def analyze_data(
    path: str,
    settings: Settings,
    operation: str,
    column: str = "",
    group_by: str = "",
    filter_expr: str = "",
    sort_by: str = "",
    limit: int = 100,
) -> ToolResult:
    """Perform data analysis operations on a CSV file."""
    try:
        p = _resolve_path(path, settings)
        if not p.is_file():
            return ToolResult(False, error=f"Not a file: {p}")
        if not _check_allowed(p, settings):
            return ToolResult(False, error="Path outside allowed roots")

        if p.suffix.lower() != ".csv":
            return ToolResult(False, error="analyze_data currently supports only CSV files")

        with p.open("r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        if not rows:
            return ToolResult(True, output="(empty CSV)", data={"row_count": 0})

        headers = reader.fieldnames or []
        if column and column not in headers:
            return ToolResult(False, error=f"Column '{column}' not found. Available: {headers}")

        # Apply filter if provided
        if filter_expr:
            rows = _filter_rows(rows, filter_expr)
            if not rows:
                return ToolResult(True, output="(no rows match filter)", data={"row_count": 0})

        if operation == "stats":
            return _analyze_stats(rows, column, headers)
        elif operation == "groupby":
            return _analyze_groupby(rows, column, group_by, headers, limit)
        elif operation == "filter":
            return _analyze_filter(rows, filter_expr, headers, limit)
        elif operation == "sort":
            return _analyze_sort(rows, sort_by, headers, limit)
        elif operation == "correlation":
            return _analyze_correlation(rows, column, headers)
        elif operation == "unique":
            return _analyze_unique(rows, column, headers, limit)
        elif operation == "head":
            return _analyze_head(rows, headers, limit)
        else:
            return ToolResult(False, error=f"Unknown operation: {operation}. Supported: stats, groupby, filter, sort, correlation, unique, head")

    except Exception as exc:
        return ToolResult(False, error=f"Failed to analyze data: {exc}")


def _filter_rows(rows: list[dict], filter_expr: str) -> list[dict]:
    """Simple filter: column OP value (e.g., 'age > 25', 'name == John')"""
    import re
    match = re.match(r'(\w+)\s*(==|!=|>|<|>=|<=)\s*(.+)', filter_expr.strip())
    if not match:
        return rows
    col, op, val = match.groups()
    val = val.strip().strip('"\'')
    try:
        val = float(val) if '.' in val else int(val)
    except ValueError:
        pass

    ops = {
        '==': lambda a, b: a == b,
        '!=': lambda a, b: a != b,
        '>': lambda a, b: float(a) > float(b),
        '<': lambda a, b: float(a) < float(b),
        '>=': lambda a, b: float(a) >= float(b),
        '<=': lambda a, b: float(a) <= float(b),
    }
    if op not in ops:
        return rows

    filtered = []
    for row in rows:
        if col not in row:
            continue
        try:
            cell = row[col]
            if ops[op](cell, val):
                filtered.append(row)
        except (ValueError, TypeError):
            continue
    return filtered


def _analyze_stats(rows: list[dict], column: str, headers: list[str]) -> ToolResult:
    if not column:
        return ToolResult(False, error="Column required for stats operation")
    values = [row.get(column, "") for row in rows]
    stats = _compute_column_stats(values)
    stats["column"] = column
    output = [f"Statistics for '{column}':"]
    for k, v in stats.items():
        output.append(f"  {k}: {v}")
    return ToolResult(True, output="\n".join(output), data={"operation": "stats", "column": column, "statistics": stats})


def _analyze_groupby(rows: list[dict], column: str, group_by: str, headers: list[str], limit: int) -> ToolResult:
    if not column or not group_by:
        return ToolResult(False, error="Both column and group_by required for groupby operation")
    if group_by not in headers:
        return ToolResult(False, error=f"Group by column '{group_by}' not found")

    groups = {}
    for row in rows:
        gval = row.get(group_by, "")
        cval = row.get(column, "")
        if gval not in groups:
            groups[gval] = []
        try:
            groups[gval].append(float(cval))
        except (ValueError, TypeError):
            groups[gval].append(cval)

    results = []
    for gval, vals in sorted(groups.items()):
        numeric = all(isinstance(v, (int, float)) for v in vals)
        if numeric:
            results.append({
                group_by: gval,
                "count": len(vals),
                "sum": sum(vals),
                "mean": statistics.mean(vals),
                "median": statistics.median(vals),
                "min": min(vals),
                "max": max(vals),
                "std": statistics.stdev(vals) if len(vals) > 1 else 0,
            })
        else:
            freq = Counter(vals).most_common(3)
            results.append({group_by: gval, "count": len(vals), "top_values": freq})

    output = [f"Group by '{group_by}', analyze '{column}':"]
    for r in results[:limit]:
        output.append(f"  {r}")
    return ToolResult(True, output="\n".join(output), data={"operation": "groupby", "column": column, "group_by": group_by, "results": results[:limit]})


def _analyze_filter(rows: list[dict], filter_expr: str, headers: list[str], limit: int) -> ToolResult:
    if not filter_expr:
        return ToolResult(False, error="filter_expr required for filter operation")
    filtered = _filter_rows(rows, filter_expr)
    preview = filtered[:limit]
    output = [f"Filter: {filter_expr}", f"Matched: {len(filtered)} rows"]
    if preview:
        output.append("Preview:")
        for row in preview:
            output.append(f"  {row}")
    return ToolResult(True, output="\n".join(output), data={"operation": "filter", "filter": filter_expr, "matched": len(filtered), "preview": preview})


def _analyze_sort(rows: list[dict], sort_by: str, headers: list[str], limit: int) -> ToolResult:
    if not sort_by or sort_by not in headers:
        return ToolResult(False, error=f"Sort column '{sort_by}' not found or not specified")
    try:
        sorted_rows = sorted(rows, key=lambda r: float(r.get(sort_by, 0)) if r.get(sort_by, "").replace(".", "", 1).isdigit() else r.get(sort_by, ""))
    except (ValueError, TypeError):
        sorted_rows = sorted(rows, key=lambda r: r.get(sort_by, ""))
    preview = sorted_rows[:limit]
    output = [f"Sorted by '{sort_by}' (first {limit}):"]
    for row in preview:
        output.append(f"  {row}")
    return ToolResult(True, output="\n".join(output), data={"operation": "sort", "column": sort_by, "preview": preview})


def _analyze_correlation(rows: list[dict], column: str, headers: list[str]) -> ToolResult:
    if not column or column not in headers:
        return ToolResult(False, error=f"Column '{column}' not found")
    numeric_cols = []
    for h in headers:
        vals = [row.get(h, "") for row in rows]
        try:
            [float(v) for v in vals if v]
            numeric_cols.append(h)
        except (ValueError, TypeError):
            pass

    if column not in numeric_cols:
        return ToolResult(False, error=f"Column '{column}' is not numeric")

    target_vals = [float(row.get(column, 0)) for row in rows if row.get(column)]
    correlations = []
    for nc in numeric_cols:
        if nc == column:
            continue
        other_vals = [float(row.get(nc, 0)) for row in rows if row.get(nc)]
        if len(target_vals) == len(other_vals) and len(target_vals) > 1:
            try:
                corr = statistics.correlation(target_vals, other_vals)
                correlations.append({"column": nc, "correlation": round(corr, 4)})
            except statistics.StatisticsError:
                pass

    correlations.sort(key=lambda x: abs(x["correlation"]), reverse=True)
    output = [f"Correlations with '{column}':"]
    for c in correlations[:10]:
        output.append(f"  {c['column']}: {c['correlation']}")
    return ToolResult(True, output="\n".join(output), data={"operation": "correlation", "column": column, "correlations": correlations})


def _analyze_unique(rows: list[dict], column: str, headers: list[str], limit: int) -> ToolResult:
    if not column or column not in headers:
        return ToolResult(False, error=f"Column '{column}' not found or not specified")
    values = [row.get(column, "") for row in rows]
    freq = Counter(v for v in values if v)
    top = freq.most_common(limit)
    output = [f"Unique values in '{column}' (top {limit}):"]
    for val, count in top:
        output.append(f"  {val}: {count}")
    return ToolResult(True, output="\n".join(output), data={"operation": "unique", "column": column, "unique_count": len(freq), "top_values": top})


def _analyze_head(rows: list[dict], headers: list[str], limit: int) -> ToolResult:
    preview = rows[:limit]
    output = [f"First {len(preview)} rows:"]
    for row in preview:
        output.append(f"  {row}")
    return ToolResult(True, output="\n".join(output), data={"operation": "head", "preview": preview, "total_rows": len(rows)})


@dataclass
class DataOperationResult:
    success: bool
    output: str
    data: dict
    error: str = ""


def execute_data_operation(
    operation: str,
    path: str,
    settings: Settings,
    **kwargs,
) -> ToolResult:
    """Unified entry point for data operations."""
    if operation in ("load_csv", "load"):
        return load_csv(path, settings)
    elif operation in ("load_json",):
        return load_json(path, settings)
    elif operation in ("inspect",):
        return inspect_data(path, settings)
    elif operation in ("analyze", "stats", "groupby", "filter", "sort", "correlation", "unique", "head"):
        return analyze_data(path, settings, operation, **kwargs)
    else:
        return ToolResult(False, error=f"Unknown data operation: {operation}")