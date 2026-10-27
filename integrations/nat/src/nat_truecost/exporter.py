"""Synchronous, idempotent booking of NAT LLM_END events into a writable ledger."""
from collections.abc import Mapping

from nat.plugin_api import Context, TelemetryExporterBaseConfig
from nat.data_models.intermediate_step import IntermediateStepType
from nat.observability.exporter.base_exporter import BaseExporter, IsolatedAttribute
from truecost.ledger import Ledger
from truecost.pricing import cost_usd


class LedgerExporterConfig(TelemetryExporterBaseConfig, name="truecost_ledger"):
    ledger: str
    run_id: str


def as_dict(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return dict(value) if isinstance(value, Mapping) else {}


def gateway_booked(value):
    """Headers may be nested in framework-provided metadata or generation payloads."""
    value = as_dict(value)
    for key, child in value.items():
        if str(key).lower() == "x-truecost-task":
            return True
        if key == "truecost_booked" and child:
            return True
        if isinstance(child, Mapping) or hasattr(child, "model_dump"):
            if gateway_booked(child):
                return True
    return False


class SummaryExporter(BaseExporter):
    """Callback-only tracing registration; never opens a writable ledger."""

    def export(self, event):
        pass


class LedgerExporter(BaseExporter):
    _starts = IsolatedAttribute(dict)

    def __init__(self, config: LedgerExporterConfig):
        super().__init__()
        self.config = config

    def export(self, event):
        if event.event_type == IntermediateStepType.LLM_START:
            self._starts[event.UUID] = (event.event_timestamp, event.metadata)
            return
        if event.event_type != IntermediateStepType.LLM_END:
            return
        started, start_metadata = self._starts.pop(event.UUID, (None, None))
        if event.span_event_timestamp is not None:
            started = event.span_event_timestamp
        ctx = Context(self._context_state)
        metadata = {**as_dict(start_metadata), **as_dict(event.metadata)}
        data = as_dict(event.data)
        if (gateway_booked(start_metadata) or gateway_booked(metadata) or gateway_booked(data)
                or gateway_booked(ctx.metadata.to_dict())):
            return
        provided = as_dict(metadata.get("provided_metadata"))
        response = as_dict(data.get("payload"))
        response_metadata = as_dict(response.get("response_metadata"))
        model = (metadata.get("model_name") or metadata.get("model") or provided.get("model_name")
                 or provided.get("model") or response.get("model") or response_metadata.get("model_name")
                 or event.name)
        # NAT has no universal eval-item-id field on an IntermediateStep. Prefer explicit item
        # metadata, otherwise the invocation trace UUID groups all calls of one workflow task.
        task_key = (metadata.get("task_key") or metadata.get("item_id") or provided.get("task_key")
                    or provided.get("item_id") or ctx.workflow_run_id)
        if task_key is None:
            raise ValueError("NAT LLM event has neither task/item metadata nor workflow_run_id")
        usage = event.usage_info.token_usage if event.usage_info is not None else None
        cached = usage.cached_tokens if usage is not None else None
        uncached = max(usage.prompt_tokens - cached, 0) if usage is not None else None
        output = usage.completion_tokens if usage is not None else None
        row = {
            "call_id": f"nat:{self.config.run_id}:{task_key}:{event.UUID}",
            "source": "nat", "run_id": self.config.run_id, "task_key": str(task_key),
            "model": model, "role": "agent", "ts_start": started, "ts_end": event.event_timestamp,
            "input_tokens": uncached, "output_tokens": output, "cache_read_tokens": cached,
            "cache_write_tokens": 0, "reasoning_tokens": usage.reasoning_tokens if usage is not None else None,
            "latency_ms": max(0.0, 1000 * (event.event_timestamp - started)) if started is not None else None,
            "cost_usd": cost_usd(model, uncached, cached, 0, output) if usage is not None else None,
            "energy_j": None,
            "meta": {"nat_event_id": event.UUID, "workflow_run_id": ctx.workflow_run_id,
                     "energy": "unmeasured", "usage_available": usage is not None},
        }
        # Per-event connection lifetime avoids sharing SQLite connections between isolated exporters.
        db = Ledger(self.config.ledger)
        try:
            if db.one("SELECT call_id FROM calls WHERE call_id=?", (row["call_id"],)) is None:
                db.add_call(row)
        finally:
            db.conn.close()
