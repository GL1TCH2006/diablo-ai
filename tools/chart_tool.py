from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from config.settings import Settings
from core.logging_setup import get_logger
from tools.registry import ToolResult

log = get_logger("chart_tool")

_SUPPORTED_TYPES = ("line", "bar", "scatter", "histogram", "pie")
_DEFAULT_OUTPUT_DIR = "charts"
_MAX_DATA_POINTS = 10000


def _ensure_output_dir(settings: Settings) -> Path:
    """Create and return the charts output directory."""
    out_dir = settings.base_dir / _DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


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


def _load_csv_data(path: str, settings: Settings, x_col: str, y_col: str = "") -> tuple[list, list, str]:
    """Load x and y data from CSV. Returns (x_data, y_data, error_msg)."""
    try:
        p = _resolve_path(path, settings)
        if not p.is_file():
            return [], [], f"Not a file: {p}"
        if not _check_allowed(p, settings):
            return [], [], "Path outside allowed roots"

        import csv
        with p.open("r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        if not rows:
            return [], [], "CSV is empty"

        if x_col not in reader.fieldnames:
            return [], [], f"Column '{x_col}' not found. Available: {reader.fieldnames}"

        x_data = []
        y_data = []
        for row in rows:
            x_val = row.get(x_col, "")
            if x_val:
                x_data.append(x_val)
            if y_col:
                y_val = row.get(y_col, "")
                if y_val:
                    y_data.append(y_val)

        if not x_data:
            return [], [], f"No data in column '{x_col}'"

        if y_col and not y_data:
            return [], [], f"No data in column '{y_col}'"

        return x_data, y_data, ""
    except Exception as exc:
        return [], [], f"Failed to load CSV: {exc}"


def _generate_filename(chart_type: str, settings: Settings) -> Path:
    """Generate a unique filename for the chart."""
    out_dir = _ensure_output_dir(settings)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{chart_type}_{timestamp}.png"
    return out_dir / filename


def _verify_chart_artifact(path: Path) -> tuple[bool, dict]:
    """Verify a chart artifact exists and is valid."""
    evidence = {"path": str(path)}
    if not path.is_file():
        evidence["error"] = "File does not exist"
        return False, evidence

    try:
        size = path.stat().st_size
        evidence["size_bytes"] = size
        if size == 0:
            evidence["error"] = "File is empty"
            return False, evidence

        from PIL import Image
        with Image.open(path) as img:
            evidence["format"] = img.format
            evidence["mode"] = img.mode
            evidence["width"] = img.width
            evidence["height"] = img.height
            if img.width <= 0 or img.height <= 0:
                evidence["error"] = "Invalid image dimensions"
                return False, evidence
    except Exception as exc:
        evidence["error"] = f"Image verification failed: {exc}"
        return False, evidence

    evidence["valid"] = True
    return True, evidence


def generate_chart(
    chart_type: str,
    settings: Settings,
    data_path: str = "",
    x_column: str = "",
    y_column: str = "",
    x_data: list = None,
    y_data: list = None,
    labels: list = None,
    title: str = "",
    xlabel: str = "",
    ylabel: str = "",
    output_path: str = "",
    width: int = 10,
    height: int = 6,
    dpi: int = 100,
    **kwargs,
) -> ToolResult:
    """Generate a chart and save it as an artifact."""
    try:
        chart_type = chart_type.lower().strip()
        if chart_type not in _SUPPORTED_TYPES:
            return ToolResult(
                False,
                error=f"Unsupported chart type: {chart_type}. Supported: {', '.join(_SUPPORTED_TYPES)}",
            )

        # Check matplotlib availability
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            return ToolResult(False, error="matplotlib not available. Install with: pip install matplotlib")

        # Prepare data
        if data_path:
            x_vals, y_vals, err = _load_csv_data(data_path, settings, x_column, y_column)
            if err:
                return ToolResult(False, error=err)
        else:
            x_vals = x_data or []
            y_vals = y_data or []

        if not x_vals:
            return ToolResult(False, error="No x data provided")

        # Limit data points
        if len(x_vals) > _MAX_DATA_POINTS:
            x_vals = x_vals[:_MAX_DATA_POINTS]
            if y_vals:
                y_vals = y_vals[:_MAX_DATA_POINTS]

        # Determine output path
        if output_path:
            out_path = _resolve_path(output_path, settings)
            if not _check_allowed(out_path.parent, settings):
                return ToolResult(False, error="Output path outside allowed roots")
        else:
            out_path = _generate_filename(chart_type, settings)

        # Generate the chart
        fig, ax = plt.subplots(figsize=(width, height), dpi=dpi)

        try:
            if chart_type == "line":
                if y_vals:
                    ax.plot(x_vals, y_vals, marker="o", linewidth=2, markersize=4)
                else:
                    ax.plot(x_vals, marker="o", linewidth=2, markersize=4)
                ax.set_xlabel(xlabel or x_column or "X")
                ax.set_ylabel(ylabel or y_column or "Y")
            elif chart_type == "bar":
                if y_vals:
                    ax.bar(range(len(x_vals)), y_vals, tick_label=x_vals)
                    ax.set_xticks(range(len(x_vals)))
                    ax.set_xticklabels(x_vals, rotation=45, ha="right")
                else:
                    ax.bar(range(len(x_vals)), x_vals)
                    ax.set_xticks(range(len(x_vals)))
                    ax.set_xticklabels(x_vals, rotation=45, ha="right")
                ax.set_xlabel(xlabel or x_column or "Category")
                ax.set_ylabel(ylabel or y_column or "Value")
            elif chart_type == "scatter":
                if not y_vals:
                    return ToolResult(False, error="Scatter plot requires y_column or y_data")
                ax.scatter(x_vals, y_vals, alpha=0.6, s=30)
                ax.set_xlabel(xlabel or x_column or "X")
                ax.set_ylabel(ylabel or y_column or "Y")
            elif chart_type == "histogram":
                ax.hist([float(v) for v in x_vals if v], bins=kwargs.get("bins", 20), edgecolor="black", alpha=0.7)
                ax.set_xlabel(xlabel or x_column or "Value")
                ax.set_ylabel(ylabel or "Frequency")
            elif chart_type == "pie":
                if not y_vals:
                    y_vals = [1] * len(x_vals)
                # Ensure numeric values for pie chart
                y_numeric = []
                for v in y_vals:
                    try:
                        y_numeric.append(float(v))
                    except (ValueError, TypeError):
                        y_numeric.append(1.0)
                ax.pie(y_numeric, labels=x_vals, autopct="%1.1f%%", startangle=90)
                ax.set_ylabel("")

            if title:
                ax.set_title(title)

            plt.tight_layout()
            fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
        finally:
            plt.close(fig)

        # Verify the artifact
        valid, evidence = _verify_chart_artifact(out_path)
        if not valid:
            return ToolResult(
                False,
                error=f"Chart generation succeeded but verification failed: {evidence.get('error', 'unknown')}",
                data={"chart_type": chart_type, "evidence": evidence},
            )

        return ToolResult(
            True,
            output=f"Generated {chart_type} chart: {out_path}",
            data={
                "chart_type": chart_type,
                "path": str(out_path),
                "title": title,
                "x_column": x_column,
                "y_column": y_column,
                "data_points": len(x_vals),
                "evidence": evidence,
            },
        )
    except Exception as exc:
        return ToolResult(False, error=f"Chart generation failed: {exc}")


def verify_chart(path: str, settings: Settings) -> ToolResult:
    """Verify a chart artifact is valid."""
    try:
        p = _resolve_path(path, settings)
        if not _check_allowed(p, settings):
            return ToolResult(False, error="Path outside allowed roots")

        valid, evidence = _verify_chart_artifact(p)
        if valid:
            return ToolResult(
                True,
                output=f"Chart verified: {p} ({evidence['width']}x{evidence['height']}, {evidence['format']})",
                data={"verified": True, "evidence": evidence},
            )
        else:
            return ToolResult(
                False,
                error=f"Chart verification failed: {evidence.get('error', 'unknown')}",
                data={"verified": False, "evidence": evidence},
            )
    except Exception as exc:
        return ToolResult(False, error=f"Chart verification failed: {exc}")


def list_charts(settings: Settings) -> ToolResult:
    """List all generated chart artifacts."""
    try:
        out_dir = _ensure_output_dir(settings)
        charts = []
        for f in sorted(out_dir.glob("*.png"), key=lambda x: x.stat().st_mtime, reverse=True):
            stat = f.stat()
            charts.append({
                "name": f.name,
                "path": str(f),
                "size_bytes": stat.st_size,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            })

        if not charts:
            return ToolResult(True, output="No charts found", data={"charts": []})

        output = [f"Found {len(charts)} chart(s):"]
        for c in charts:
            output.append(f"  {c['name']} ({c['size_kb']} KB, {c['modified']})")

        return ToolResult(True, output="\n".join(output), data={"charts": charts})
    except Exception as exc:
        return ToolResult(False, error=f"Failed to list charts: {exc}")


def delete_chart(path: str, settings: Settings) -> ToolResult:
    """Delete a chart artifact."""
    try:
        p = _resolve_path(path, settings)
        if not _check_allowed(p, settings):
            return ToolResult(False, error="Path outside allowed roots")
        if not p.is_file():
            return ToolResult(False, error=f"Not a file: {p}")
        if p.suffix.lower() != ".png":
            return ToolResult(False, error="Only PNG files can be deleted via this tool")

        p.unlink()
        return ToolResult(True, output=f"Deleted chart: {p}", data={"deleted": str(p)})
    except Exception as exc:
        return ToolResult(False, error=f"Failed to delete chart: {exc}")