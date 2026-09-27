"""M2.14 Phase 5 — Data/Chart Knowledge Integration.

Integrates M2.13.2 data analysis and chart generation with the knowledge graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.knowledge_graph import (
    KnowledgeGraph,
    NodeType,
    RelationType,
    canonical_dataset_key,
    canonical_chart_key,
    canonical_artifact_key,
    canonical_analysis_key,
    canonical_file_key,
)
from core.knowledge_tasks import link_verification_to_evidence
from core.logging_setup import get_logger
from core.verification_ledger import (
    EvidenceTrust,
    VerificationEntry,
    VerificationMethod,
    VerificationState,
    VerificationSubject,
)

import time

log = get_logger("knowledge_data")


@dataclass
class DatasetInfo:
    path: str
    format: str  # "csv" or "json"
    columns: list[str]
    row_count: int
    column_types: dict[str, str] | None = None


@dataclass
class ChartInfo:
    path: str
    chart_type: str
    title: str
    x_column: str
    y_column: str | None
    data_points: int
    dataset_path: str | None = None


@dataclass
class AnalysisInfo:
    dataset_path: str
    operation: str  # "stats", "groupby", "filter", "sort", "correlation", "unique", "head"
    column: str
    extra_params: dict


def normalize_dataset_result(result_data: dict) -> dict:
    """Normalise the two legitimate data-tool result shapes for the graph.

    ``data.load_csv`` reports per-column ``column_types`` and ``data.inspect``
    reports per-column ``column_stats``. Both describe the same thing, so both
    are preserved as published and normalised *here*, at the projection
    boundary, rather than by changing either tool's public contract.

    Anything unrecognised is simply absent from the result; the projection
    records only what it can actually observe.
    """
    if not isinstance(result_data, dict):
        return {"columns": [], "row_count": 0, "column_types": {}}

    columns = result_data.get("headers")
    if not isinstance(columns, (list, tuple)):
        columns = []

    row_count = result_data.get("row_count", 0)
    try:
        row_count = int(row_count)
    except (TypeError, ValueError):
        row_count = 0

    # column_types (load_csv) and column_stats (inspect) both carry name+type.
    types: dict[str, str] = {}
    for key in ("column_types", "column_stats"):
        entries = result_data.get(key)
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "") or "")
            kind = str(entry.get("type", "") or "")
            if name and kind:
                types[name] = kind

    fmt = result_data.get("format")
    if not fmt:
        raw_path = str(result_data.get("path", "") or "")
        fmt = Path(raw_path).suffix.lstrip(".").lower() if raw_path else ""

    return {
        "columns": [str(c) for c in columns],
        "row_count": row_count,
        "column_types": types,
        "format": str(fmt),
    }


def upsert_dataset_node(graph: KnowledgeGraph, info: DatasetInfo) -> str:
    """Create or update a DATASET node.

    Returns the dataset node_id.
    """
    ds_key = canonical_dataset_key(info.path)
    metadata = {
        "path": info.path,
        "format": info.format,
        "columns": info.columns,
        "row_count": info.row_count,
        "column_types": info.column_types or {},
    }
    node = graph.upsert_node(NodeType.DATASET, ds_key, Path(info.path).name, metadata)
    return node.node_id


def upsert_chart_node(graph: KnowledgeGraph, info: ChartInfo) -> str:
    """Create or update a CHART node.

    Returns the chart node_id.
    """
    chart_key = canonical_chart_key(info.path)
    metadata = {
        "path": info.path,
        "chart_type": info.chart_type,
        "title": info.title,
        "x_column": info.x_column,
        "y_column": info.y_column or "",
        "data_points": info.data_points,
        "dataset_path": info.dataset_path or "",
    }
    node = graph.upsert_node(NodeType.CHART, chart_key, Path(info.path).name, metadata)
    return node.node_id


def upsert_analysis_node(graph: KnowledgeGraph, info: AnalysisInfo) -> str:
    """Create (or refresh) the ANALYSIS node for one analysis question.

    Identity is the question itself, so asking the same question again updates
    the existing node instead of accumulating duplicates. The stored metadata is
    deliberately deterministic: it records what was asked, never the values the
    analysis returned.
    """
    params = info.extra_params or {}
    metadata = {
        "operation": info.operation,
        "dataset_path": info.dataset_path,
    }
    if info.column:
        metadata["column"] = info.column
    if params:
        metadata["params"] = {str(k): str(v) for k, v in sorted(params.items())}
    node = graph.upsert_node(
        NodeType.ANALYSIS,
        canonical_analysis_key(info.dataset_path, info.operation, info.column, params),
        f"{info.operation} on {info.dataset_path}",
        metadata,
    )
    return node.node_id


def link_dataset_to_file(graph: KnowledgeGraph, dataset_node_id: str, file_node_id: str) -> None:
    """Link DATASET -> DERIVED_FROM -> FILE."""
    graph.upsert_edge(dataset_node_id, file_node_id, RelationType.DERIVED_FROM)


def link_chart_to_dataset(graph: KnowledgeGraph, chart_node_id: str, dataset_node_id: str) -> None:
    """Link CHART -> GENERATED_FROM -> DATASET."""
    graph.upsert_edge(chart_node_id, dataset_node_id, RelationType.GENERATED_FROM)


def link_chart_to_artifact(graph: KnowledgeGraph, chart_node_id: str, artifact_node_id: str) -> None:
    """Link CHART -> PRODUCED -> ARTIFACT (the chart image file)."""
    graph.upsert_edge(chart_node_id, artifact_node_id, RelationType.PRODUCED)


def link_task_to_chart(graph: KnowledgeGraph, task_node_id: str, chart_node_id: str) -> None:
    """Link TASK -> PRODUCED -> CHART. No-op without a task anchor."""
    if not task_node_id:
        return
    graph.upsert_edge(task_node_id, chart_node_id, RelationType.PRODUCED)


def link_task_to_dataset(graph: KnowledgeGraph, task_node_id: str, dataset_node_id: str) -> None:
    """Link TASK -> PRODUCED -> DATASET. No-op without a task anchor."""
    if not task_node_id:
        return
    graph.upsert_edge(task_node_id, dataset_node_id, RelationType.PRODUCED)


def link_analysis_to_dataset(graph: KnowledgeGraph, analysis_node_id: str, dataset_node_id: str) -> None:
    """Link ANALYSIS -> DERIVED_FROM -> DATASET."""
    graph.upsert_edge(analysis_node_id, dataset_node_id, RelationType.DERIVED_FROM)


def link_task_to_analysis(graph: KnowledgeGraph, task_node_id: str, analysis_node_id: str) -> None:
    """Link TASK -> PRODUCED -> ANALYSIS. No-op without a task anchor."""
    if not task_node_id:
        return
    graph.upsert_edge(task_node_id, analysis_node_id, RelationType.PRODUCED)


# High-level sync functions for M2.13.2 tool results


def sync_dataset_inspection(
    graph: KnowledgeGraph,
    task_node_id: str,
    file_path: str,
    result_data: dict,
) -> Optional[str]:
    """Sync a dataset observation to the graph.

    Accepts either legitimate data-tool result shape (``data.load_csv``'s
    ``column_types`` or ``data.inspect``'s ``column_stats``) via
    :func:`normalize_dataset_result`.

    ``file_path`` is the authoritative path the tool was invoked with, not a
    path reconstructed from model text.
    """
    try:
        shape = normalize_dataset_result(result_data or {})
        info = DatasetInfo(
            path=file_path,
            format=shape["format"],
            columns=shape["columns"],
            row_count=shape["row_count"],
            column_types=shape["column_types"],
        )
        ds_node_id = upsert_dataset_node(graph, info)

        # Link to source file, if that file is indexed.
        file_key = canonical_file_key(graph.project_id, file_path)
        file_node = graph.get_node(file_key)
        if file_node is not None:
            link_dataset_to_file(graph, ds_node_id, file_node.node_id)

        # Link to task
        link_task_to_dataset(graph, task_node_id, ds_node_id)

        log.info("Synced dataset %s to graph", file_path)
        return ds_node_id
    except Exception as exc:
        log.warning("Failed to sync dataset %s: %s", file_path, exc)
        return None


def sync_data_analysis(
    graph: KnowledgeGraph,
    task_node_id: str,
    dataset_path: str,
    operation: str,
    column: str,
    result_data: dict,
    extra_params: dict | None = None,
) -> Optional[str]:
    """Sync data analysis result to graph.

    Args:
        graph: KnowledgeGraph instance
        task_node_id: Task that performed the analysis
        dataset_path: Path to the source dataset
        operation: Analysis operation ("stats", "groupby", "filter", etc.)
        column: Column analyzed
        result_data: Result from data_tool.analyze_data()
        extra_params: Additional parameters (group_by, filter_expr, etc.)

    Returns:
        Analysis node_id or None if failed
    """
    try:
        # Get or create dataset node
        ds_key = canonical_dataset_key(dataset_path)
        ds_node = graph.get_node(ds_key)
        ds_node_id = ds_node.node_id if ds_node else None

        info = AnalysisInfo(
            dataset_path=dataset_path,
            operation=operation,
            column=column,
            extra_params=extra_params or {},
        )
        analysis_id = upsert_analysis_node(graph, info)

        if ds_node_id:
            link_analysis_to_dataset(graph, analysis_id, ds_node_id)
        link_task_to_analysis(graph, task_node_id, analysis_id)

        log.info("Synced analysis %s on %s to graph", operation, dataset_path)
        return analysis_id
    except Exception as exc:
        log.warning("Failed to sync analysis %s on %s: %s", operation, dataset_path, exc)
        return None


def sync_chart_generation(
    graph: KnowledgeGraph,
    task_node_id: str,
    chart_path: str,
    chart_type: str,
    dataset_path: str | None,
    result_data: dict,
) -> Optional[str]:
    """Sync chart generation result to graph.

    Args:
        graph: KnowledgeGraph instance
        task_node_id: Task that generated the chart
        chart_path: Path to the generated chart file
        chart_type: Type of chart ("line", "bar", "scatter", etc.)
        dataset_path: Path to the source dataset (if any)
        result_data: Result from chart_tool.generate_chart()

    Returns:
        Chart node_id or None if failed
    """
    try:
        info = ChartInfo(
            path=chart_path,
            chart_type=chart_type,
            title=result_data.get("title", ""),
            x_column=result_data.get("x_column", ""),
            y_column=result_data.get("y_column"),
            data_points=result_data.get("data_points", 0),
            dataset_path=dataset_path,
        )
        chart_node_id = upsert_chart_node(graph, info)

        # Link to artifact (the chart file itself)
        art_key = canonical_artifact_key(chart_path)
        art_node = graph.upsert_node(NodeType.ARTIFACT, art_key, Path(chart_path).name, {"path": chart_path})
        link_chart_to_artifact(graph, chart_node_id, art_node.node_id)

        # Link to dataset if provided
        if dataset_path:
            ds_key = canonical_dataset_key(dataset_path)
            ds_node = graph.get_node(ds_key)
            if ds_node:
                link_chart_to_dataset(graph, chart_node_id, ds_node.node_id)

        # Link to task
        link_task_to_chart(graph, task_node_id, chart_node_id)

        log.info("Synced chart %s to graph", chart_path)
        return chart_node_id
    except Exception as exc:
        log.warning("Failed to sync chart %s: %s", chart_path, exc)
        return None


def sync_chart_verification(
    graph: KnowledgeGraph,
    chart_path: str,
    verified: bool,
    evidence: dict,
) -> Optional[str]:
    """Sync chart verification result to graph.

    Creates a VERIFICATION node linked to the chart.

    Returns:
        Verification node_id or None if failed
    """
    try:
        chart_key = canonical_chart_key(chart_path)
        chart_node = graph.get_node(chart_key)
        if not chart_node:
            return None

        entry = VerificationEntry.create(
            state=VerificationState.PASSED if verified else VerificationState.FAILED,
            subject_kind=VerificationSubject.FILE,
            method_kind=VerificationMethod.IMAGE_INSPECTION,
            trust=EvidenceTrust.OBSERVED,
            detail="Chart artifact verified" if verified else "Chart artifact verification failed",
            evidence=evidence,
            legacy_kind="chart_verification",
        )

        verif_key = f"verification:chart:{chart_path}:{int(time.time())}"
        metadata = {
            "chart_path": chart_path,
            "state": entry.state.value,
            "detail": entry.detail,
            "evidence": entry.evidence,
            "recorded_at": entry.recorded_at,
        }
        verif_node = graph.upsert_node(NodeType.VERIFICATION, verif_key, f"Chart verification: {chart_path}", metadata)

        link_verification_to_evidence(graph, verif_node.node_id, chart_node.node_id)

        log.info("Synced chart verification for %s: %s", chart_path, "passed" if verified else "failed")
        return verif_node.node_id
    except Exception as exc:
        log.warning("Failed to sync chart verification for %s: %s", chart_path, exc)
        return None