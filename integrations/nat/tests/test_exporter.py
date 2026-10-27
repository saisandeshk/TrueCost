import asyncio
import json
from pathlib import Path

import pytest
from nat.plugin_api import Context, ContextState
from nat.data_models.intermediate_step import IntermediateStep, IntermediateStepPayload, UsageInfo, StreamEventData
from nat.data_models.invocation_node import InvocationNode
from nat.data_models.token_usage import TokenUsageBaseModel
from truecost.ledger import Ledger
from truecost.pricing import cost_usd

from nat_truecost.exporter import LedgerExporter, LedgerExporterConfig, SummaryExporter


def step(event_type="LLM_END", **kwargs):
    fields = dict(event_type=event_type, UUID="call-1", event_timestamp=12.5,
                  name="qwen/qwen3.5-9b", metadata={"item_id": "task-1"},
                  usage_info=UsageInfo(token_usage=TokenUsageBaseModel(
                      prompt_tokens=100, cached_tokens=30, completion_tokens=20, reasoning_tokens=5)))
    fields.update(kwargs)
    return IntermediateStep(parent_id="root", function_ancestry=InvocationNode(
        function_id="root", function_name="test"), payload=IntermediateStepPayload(**fields))


def exporter(tmp_path):
    return LedgerExporter(LedgerExporterConfig(ledger=str(tmp_path / "calls.db"), run_id="nat-run"))


def test_exporter_subscribes_books_prices_once_and_unsubscribes(tmp_path):
    exp = exporter(tmp_path)

    async def check():
        with Context.scope(workflow_run_id="trace-1"):
            async with exp.start():
                stream = ContextState.get().event_stream.get()
                stream.on_next(step("LLM_START", event_timestamp=10))
                stream.on_next(step())
                stream.on_next(step())
            stream.on_next(step(UUID="after-stop"))
    asyncio.run(check())
    db = Ledger(tmp_path / "calls.db")
    try:
        rows = db.query("SELECT * FROM calls")
        assert len(rows) == 1
        row = rows[0]
        assert row["source"] == "nat" and row["run_id"] == "nat-run"
        assert row["task_key"] == "task-1" and row["role"] == "agent"
        assert row["input_tokens"] == 70 and row["cache_read_tokens"] == 30
        assert row["output_tokens"] == 20 and row["reasoning_tokens"] == 5
        assert row["latency_ms"] == 2500
        assert row["energy_j"] is None
        assert row["cost_usd"] is not None
        assert row["cost_usd"] == cost_usd("qwen/qwen3.5-9b", 70, 30, 0, 20)
        assert json.loads(row["meta"])["usage_available"]
    finally:
        db.conn.close()


@pytest.mark.parametrize("metadata,data", [
    ({"headers": {"x-TRUEcost-TaSk": "task-1"}}, None),
    ({"provided_metadata": {"headers": {"X-TrueCost-Task": "task-1"}}}, None),
    ({"truecost_booked": True}, None),
    ({}, StreamEventData(payload={"headers": {"X-TrueCost-Task": "task-1"}})),
])
def test_gateway_booked_events_do_not_even_open_ledger(tmp_path, metadata, data):
    exp = exporter(tmp_path)
    exp.export(step(metadata=metadata, data=data))
    assert not Path(exp.config.ledger).exists()


def test_start_headers_also_prevent_double_booking(tmp_path):
    exp = exporter(tmp_path)
    exp.export(step("LLM_START", metadata={"headers": {"X-TrueCost-Task": "task-1"}}))
    exp.export(step())
    assert not Path(exp.config.ledger).exists()


def test_trace_fallback_and_unknown_usage_and_model(tmp_path):
    exp = exporter(tmp_path)
    with Context.scope(workflow_run_id="trace-1"):
        exp.export(step(metadata={}, name="unknown-model", usage_info=None))
    db = Ledger(exp.config.ledger)
    try:
        row = db.one("SELECT * FROM calls")
        assert row["task_key"] == "trace-1"
        assert row["cost_usd"] is None and row["energy_j"] is None
        assert row["input_tokens"] is None and row["latency_ms"] is None
    finally:
        db.conn.close()


def test_model_and_task_from_provided_metadata(tmp_path):
    exp = exporter(tmp_path)
    exp.export(step(metadata={"provided_metadata": {"model_name": "qwen/qwen3.5-9b", "task_key": "external/1"}},
                    name="provider-wrapper", span_event_timestamp=11))
    db = Ledger(exp.config.ledger)
    try:
        row = db.one("SELECT * FROM calls")
        assert row["model"] == "qwen/qwen3.5-9b" and row["task_key"] == "external/1"
        assert row["latency_ms"] == 1500 and row["cost_usd"] is not None
    finally:
        db.conn.close()


def test_non_llm_and_summary_exporter_never_write(tmp_path):
    exp = exporter(tmp_path)
    exp.export(step("TOOL_END"))
    SummaryExporter().export(step())
    assert not Path(exp.config.ledger).exists()


def test_missing_attribution_rejected(tmp_path):
    exp = exporter(tmp_path)
    with Context.scope(workflow_run_id=None):
        with pytest.raises(ValueError, match="task/item"):
            exp.export(step(metadata={}))
    assert not Path(exp.config.ledger).exists()


def test_isolated_exporters_do_not_share_start_maps(tmp_path):
    exp = exporter(tmp_path)
    other = exp.create_isolated_instance(ContextState.get())
    exp.export(step("LLM_START", event_timestamp=1))
    assert "call-1" in exp._starts and "call-1" not in other._starts


def test_request_headers_prevent_double_booking(tmp_path):
    from types import SimpleNamespace
    exp = exporter(tmp_path)
    request = SimpleNamespace(to_dict=lambda: {"headers": {"X-TrueCost-Task": "booked"}})
    with Context.scope(_metadata=request):
        exp.export(step())
    assert not Path(exp.config.ledger).exists()
