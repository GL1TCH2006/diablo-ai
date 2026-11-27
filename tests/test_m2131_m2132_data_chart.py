"""M2.13.1/M2.13.2 - Data and chart tool tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from config.permissions import PermissionManager
from config.settings import Settings
from core.agent import Agent
from core.events import EventBus
from memory.preferences import PreferencesStore
from tools import build_registry


def _settings(tmp: Path) -> Settings:
    data = tmp / "data"
    return Settings(
        base_dir=tmp,
        ai_provider="ollama",
        ollama_host="http://x",
        ai_model="",
        workspace_root=tmp / "p",
        data_dir=data,
        memory_dir=data / "memory",
        logs_dir=data / "logs",
        backups_dir=data / "backups",
        scratch_dir=tmp / "w",
        sessions_dir=data / "sessions",
        voice_enabled=False,
        log_level="INFO",
        allowed_roots=[tmp],
        raw={},
    )


def _agent(tmp: Path):
    prefs = PreferencesStore(_settings(tmp).memory_dir)
    return Agent(
        provider=type("P", (), {
            "model_label": "test",
            "name": "test",
            "chat": lambda self, messages, tools=None, model=None: type("R", (), {"text": "ok", "tool_calls": []})(),
        })(),
        settings=_settings(tmp),
        registry=build_registry(_settings(tmp)),
        permissions=PermissionManager(_settings(tmp), prefs),
        events=EventBus(),
        confirmer=None,
        project=None,
        memory_preferences=prefs,
        model_name="",
    )


class TestDataTools:
    """Tests for data analysis tools."""

    def test_load_csv(self, tmp_path: Path):
        """Load a CSV file and return its structure."""
        csv_content = "name,age,score\nAlice,25,95\nBob,30,87\nCharlie,35,92\n"
        p = tmp_path / "test.csv"
        p.write_text(csv_content)

        from tools.data_tool import load_csv
        result = load_csv(str(p), _settings(tmp_path))

        assert result.success
        assert "Rows: 3" in result.output
        assert "name" in result.output
        assert "age" in result.output
        assert "score" in result.output
        assert result.data["row_count"] == 3
        assert result.data["headers"] == ["name", "age", "score"]

    def test_load_json(self, tmp_path: Path):
        """Load a JSON file and return its structure."""
        json_content = '{"users": [{"name": "Alice", "age": 25}], "config": {"debug": true}}'
        p = tmp_path / "test.json"
        p.write_text(json_content)

        from tools.data_tool import load_json
        result = load_json(str(p), _settings(tmp_path))

        assert result.success
        assert "Object with 2 keys" in result.output
        assert "users" in result.output
        assert "config" in result.output

    def test_inspect_csv(self, tmp_path: Path):
        """Inspect a CSV file for statistics."""
        csv_content = "name,age,score\nAlice,25,95\nBob,30,87\nCharlie,35,92\n"
        p = tmp_path / "test.csv"
        p.write_text(csv_content)

        from tools.data_tool import inspect_data
        result = inspect_data(str(p), _settings(tmp_path))

        assert result.success
        assert "Rows: 3 data rows" in result.output
        assert "Columns: 3" in result.output
        assert "age" in result.output
        assert "score" in result.output
        assert "integer" in result.output  # numeric column type detected

    def test_inspect_json(self, tmp_path: Path):
        """Inspect a JSON file."""
        json_content = '{"users": [{"name": "Alice"}], "count": 1}'
        p = tmp_path / "test.json"
        p.write_text(json_content)

        from tools.data_tool import inspect_data
        result = inspect_data(str(p), _settings(tmp_path))

        assert result.success
        assert "Object with 2 keys" in result.output

    def test_analyze_stats(self, tmp_path: Path):
        """Analyze column statistics."""
        csv_content = "name,age,score\nAlice,25,95\nBob,30,87\nCharlie,35,92\n"
        p = tmp_path / "test.csv"
        p.write_text(csv_content)

        from tools.data_tool import analyze_data
        result = analyze_data(str(p), _settings(tmp_path), "stats", column="age")

        assert result.success
        assert "Statistics for 'age'" in result.output
        assert "mean" in result.data["statistics"]
        assert result.data["statistics"]["count"] == 3

    def test_analyze_groupby(self, tmp_path: Path):
        """Group by a column and compute aggregates."""
        csv_content = "dept,salary\nEngineering,100\nEngineering,120\nSales,80\nSales,90\n"
        p = tmp_path / "test.csv"
        p.write_text(csv_content)

        from tools.data_tool import analyze_data
        result = analyze_data(str(p), _settings(tmp_path), "groupby", column="salary", group_by="dept")

        assert result.success
        assert "Engineering" in result.output
        assert "Sales" in result.output
        assert result.data["results"][0]["count"] == 2

    def test_analyze_filter(self, tmp_path: Path):
        """Filter rows by expression."""
        csv_content = "name,age\nAlice,25\nBob,30\nCharlie,35\n"
        p = tmp_path / "test.csv"
        p.write_text(csv_content)

        from tools.data_tool import analyze_data
        result = analyze_data(str(p), _settings(tmp_path), "filter", filter_expr="age > 28")

        assert result.success
        assert "Matched: 2 rows" in result.output
        assert "Bob" in result.output
        assert "Charlie" in result.output

    def test_analyze_sort(self, tmp_path: Path):
        """Sort by column."""
        csv_content = "name,age\nCharlie,35\nAlice,25\nBob,30\n"
        p = tmp_path / "test.csv"
        p.write_text(csv_content)

        from tools.data_tool import analyze_data
        result = analyze_data(str(p), _settings(tmp_path), "sort", sort_by="age", limit=5)

        assert result.success
        assert "Alice" in result.output
        assert "Bob" in result.output
        assert "Charlie" in result.output

    def test_analyze_correlation(self, tmp_path: Path):
        """Compute correlations with a target column."""
        csv_content = "x,y,z\n1,2,3\n2,4,6\n3,6,9\n4,8,12\n"
        p = tmp_path / "test.csv"
        p.write_text(csv_content)

        from tools.data_tool import analyze_data
        result = analyze_data(str(p), _settings(tmp_path), "correlation", column="y")

        assert result.success
        assert "Correlations with 'y'" in result.output

    def test_analyze_unique(self, tmp_path: Path):
        """Find unique values in a column."""
        csv_content = "dept,name\nEngineering,Alice\nEngineering,Bob\nSales,Charlie\n"
        p = tmp_path / "test.csv"
        p.write_text(csv_content)

        from tools.data_tool import analyze_data
        result = analyze_data(str(p), _settings(tmp_path), "unique", column="dept")

        assert result.success
        assert "Engineering" in result.output
        assert "Sales" in result.output
        assert result.data["unique_count"] == 2

    def test_analyze_head(self, tmp_path: Path):
        """Get first N rows."""
        csv_content = "name,age\nAlice,25\nBob,30\nCharlie,35\nDavid,40\n"
        p = tmp_path / "test.csv"
        p.write_text(csv_content)

        from tools.data_tool import analyze_data
        result = analyze_data(str(p), _settings(tmp_path), "head", limit=2)

        assert result.success
        assert "2 rows" in result.output
        assert len(result.data["preview"]) == 2

    def test_execute_data_operation_dispatch(self, tmp_path: Path):
        """Unified entry point dispatches correctly."""
        csv_content = "name,age\nAlice,25\nBob,30\n"
        p = tmp_path / "test.csv"
        p.write_text(csv_content)

        from tools.data_tool import execute_data_operation

        # Test load_csv
        result = execute_data_operation("load_csv", str(p), _settings(tmp_path))
        assert result.success

        # Test inspect
        result = execute_data_operation("inspect", str(p), _settings(tmp_path))
        assert result.success

        # Test analyze stats
        result = execute_data_operation("stats", str(p), _settings(tmp_path), column="age")
        assert result.success


class TestChartTools:
    """Tests for chart generation tools."""

    def test_generate_line_chart(self, tmp_path: Path):
        """Generate a line chart from CSV data."""
        csv_content = "x,y\n1,2\n2,4\n3,6\n4,8\n"
        p = tmp_path / "data.csv"
        p.write_text(csv_content)

        from tools.chart_tool import generate_chart
        result = generate_chart("line", _settings(tmp_path), data_path=str(p), x_column="x", y_column="y")

        assert result.success
        assert "Generated line chart" in result.output
        assert result.data["chart_type"] == "line"
        assert result.data["data_points"] == 4
        assert result.data["evidence"]["valid"] is True

    def test_generate_bar_chart(self, tmp_path: Path):
        """Generate a bar chart from CSV data."""
        csv_content = "category,value\nA,10\nB,20\nC,15\n"
        p = tmp_path / "data.csv"
        p.write_text(csv_content)

        from tools.chart_tool import generate_chart
        result = generate_chart("bar", _settings(tmp_path), data_path=str(p), x_column="category", y_column="value")

        assert result.success
        assert "Generated bar chart" in result.output
        assert result.data["chart_type"] == "bar"

    def test_generate_scatter_chart(self, tmp_path: Path):
        """Generate a scatter chart."""
        csv_content = "x,y\n1,2\n2,3\n3,5\n4,7\n"
        p = tmp_path / "data.csv"
        p.write_text(csv_content)

        from tools.chart_tool import generate_chart
        result = generate_chart("scatter", _settings(tmp_path), data_path=str(p), x_column="x", y_column="y")

        assert result.success
        assert "Generated scatter chart" in result.output
        assert result.data["chart_type"] == "scatter"

    def test_generate_histogram(self, tmp_path: Path):
        """Generate a histogram."""
        csv_content = "value\n1\n2\n2\n3\n3\n3\n4\n4\n5\n"
        p = tmp_path / "data.csv"
        p.write_text(csv_content)

        from tools.chart_tool import generate_chart
        result = generate_chart("histogram", _settings(tmp_path), data_path=str(p), x_column="value")

        assert result.success
        assert "Generated histogram chart" in result.output
        assert result.data["chart_type"] == "histogram"

    def test_generate_pie_chart(self, tmp_path: Path):
        """Generate a pie chart."""
        csv_content = "category,count\nA,10\nB,20\nC,30\n"
        p = tmp_path / "data.csv"
        p.write_text(csv_content)

        from tools.chart_tool import generate_chart
        result = generate_chart("pie", _settings(tmp_path), data_path=str(p), x_column="category", y_column="count")

        assert result.success
        assert "Generated pie chart" in result.output
        assert result.data["chart_type"] == "pie"

    def test_generate_chart_unsupported_type(self, tmp_path: Path):
        """Unsupported chart type returns error."""
        from tools.chart_tool import generate_chart
        result = generate_chart("radar", _settings(tmp_path))

        assert not result.success
        assert "Unsupported chart type" in result.error
        assert "radar" in result.error

    def test_generate_chart_missing_y_for_scatter(self, tmp_path: Path):
        """Scatter chart requires y_column."""
        csv_content = "x\n1\n2\n3\n"
        p = tmp_path / "data.csv"
        p.write_text(csv_content)

        from tools.chart_tool import generate_chart
        result = generate_chart("scatter", _settings(tmp_path), data_path=str(p), x_column="x")

        assert not result.success
        assert "requires y_column" in result.error

    def test_generate_chart_inline_data(self, tmp_path: Path):
        """Generate chart from inline data."""
        from tools.chart_tool import generate_chart
        result = generate_chart("line", _settings(tmp_path), x_data=["1", "2", "3"], y_data=["2", "4", "6"])

        assert result.success
        assert "Generated line chart" in result.output

    def test_verify_chart(self, tmp_path: Path):
        """Verify a chart artifact."""
        from tools.chart_tool import generate_chart, verify_chart

        csv_content = "x,y\n1,2\n2,4\n"
        p = tmp_path / "data.csv"
        p.write_text(csv_content)

        gen_result = generate_chart("line", _settings(tmp_path), data_path=str(p), x_column="x", y_column="y")
        assert gen_result.success
        chart_path = gen_result.data["path"]

        verify_result = verify_chart(chart_path, _settings(tmp_path))
        assert verify_result.success
        assert "Chart verified" in verify_result.output
        assert verify_result.data["verified"] is True

    def test_verify_chart_invalid(self, tmp_path: Path):
        """Verify a non-existent chart."""
        from tools.chart_tool import verify_chart

        p = tmp_path / "nonexistent.png"
        result = verify_chart(str(p), _settings(tmp_path))
        assert not result.success
        assert "does not exist" in result.error or "verification failed" in result.error

    def test_list_charts(self, tmp_path: Path):
        """List chart artifacts."""
        from tools.chart_tool import generate_chart, list_charts

        csv_content = "x,y\n1,2\n"
        p = tmp_path / "data.csv"
        p.write_text(csv_content)

        gen_result = generate_chart("line", _settings(tmp_path), data_path=str(p), x_column="x", y_column="y")
        assert gen_result.success

        list_result = list_charts(_settings(tmp_path))
        assert list_result.success
        assert len(list_result.data["charts"]) >= 1

    def test_delete_chart(self, tmp_path: Path):
        """Delete a chart artifact."""
        from tools.chart_tool import generate_chart, delete_chart

        csv_content = "x,y\n1,2\n"
        p = tmp_path / "data.csv"
        p.write_text(csv_content)

        gen_result = generate_chart("line", _settings(tmp_path), data_path=str(p), x_column="x", y_column="y")
        assert gen_result.success
        chart_path = gen_result.data["path"]

        del_result = delete_chart(chart_path, _settings(tmp_path))
        assert del_result.success
        assert "Deleted chart" in del_result.output


class TestDataChartIntegration:
    """Integration tests for data + chart workflow."""

    def test_data_analysis_then_chart(self, tmp_path: Path):
        """Load data, analyze, then generate chart."""
        csv_content = "month,sales\nJan,100\nFeb,150\nMar,200\nApr,180\n"
        p = tmp_path / "sales.csv"
        p.write_text(csv_content)

        from tools.data_tool import inspect_data, analyze_data
        from tools.chart_tool import generate_chart, verify_chart

        # Inspect
        inspect_result = inspect_data(str(p), _settings(tmp_path))
        assert inspect_result.success

        # Analyze stats
        stats_result = analyze_data(str(p), _settings(tmp_path), "stats", column="sales")
        assert stats_result.success

        # Generate chart
        chart_result = generate_chart("line", _settings(tmp_path), data_path=str(p), x_column="month", y_column="sales", title="Monthly Sales")
        assert chart_result.success

        # Verify chart
        verify_result = verify_chart(chart_result.data["path"], _settings(tmp_path))
        assert verify_result.success
        assert verify_result.data["verified"] is True


class TestToolContextForDataChart:
    """Test that tool context selector includes data/chart capabilities."""

    def test_data_context_selected(self, tmp_path: Path):
        """Data-related request selects data capability."""
        from core.tool_context import ToolContextSelector
        from tools import build_registry

        registry = build_registry(_settings(tmp_path))
        selector = ToolContextSelector(registry)

        # Request data capability
        decision = selector.select(capabilities=("data",))
        assert "data" in decision.capabilities
        assert any(t.startswith("data.") for t in decision.tool_names)

    def test_chart_context_selected(self, tmp_path: Path):
        """Chart-related request selects chart capability."""
        from core.tool_context import ToolContextSelector
        from tools import build_registry

        registry = build_registry(_settings(tmp_path))
        selector = ToolContextSelector(registry)

        # Request chart capability
        decision = selector.select(capabilities=("chart",))
        assert "chart" in decision.capabilities
        assert any(t.startswith("chart.") for t in decision.tool_names)

    def test_combined_data_chart_context(self, tmp_path: Path):
        """Combined data+chart request includes both."""
        from core.tool_context import ToolContextSelector
        from tools import build_registry

        registry = build_registry(_settings(tmp_path))
        selector = ToolContextSelector(registry)

        decision = selector.select(capabilities=("data", "chart"))
        assert "data" in decision.capabilities
        assert "chart" in decision.capabilities
        assert any(t.startswith("data.") for t in decision.tool_names)
        assert any(t.startswith("chart.") for t in decision.tool_names)