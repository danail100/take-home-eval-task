"""The eval side of the OTel setup: configure the SDK, collect spans, and serialize them.

The assistant emits spans through the OTel API. Here we install a TracerProvider that
  * collects every finished span in memory, to be written to `spans.jsonl`, and
  * optionally also exports them over OTLP (to Langfuse, Phoenix, Jaeger and so on)
    when OTEL_EXPORTER_OTLP_ENDPOINT is set and the `otlp` extra is installed.

Stored spans follow the OTLP/JSON field names (traceId, spanId, parentSpanId, kind,
startTimeUnixNano and so on). The one simplification is that attributes are a flat
{key: value} map instead of the verbose OTLP key/value list.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Sequence

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

eval_tracer = trace.get_tracer("evalkit", "0.1.0")

# Eval-side attributes, set on the `eval.trial` span that wraps each agent invocation.
RUN_ID = "eval.run.id"
TASK_ID = "eval.task.id"
TRIAL = "eval.trial"
SEED = "eval.seed"


class CollectingExporter(SpanExporter):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            self._spans.extend(spans)
        return SpanExportResult.SUCCESS

    def drain(self) -> list[ReadableSpan]:
        with self._lock:
            out, self._spans = self._spans, []
        return out

    def shutdown(self) -> None:
        pass


_provider: TracerProvider | None = None
_collector: CollectingExporter | None = None


def setup_tracing() -> CollectingExporter:
    """Install the SDK TracerProvider once per process and return the collector."""
    global _provider, _collector
    if _collector is not None:
        return _collector
    _provider = TracerProvider(resource=Resource.create({"service.name": "assistant-eval"}))
    _collector = CollectingExporter()
    _provider.add_span_processor(SimpleSpanProcessor(_collector))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        except ImportError:
            print("warning: OTEL_EXPORTER_OTLP_ENDPOINT set but the otlp extra is not installed")
    trace.set_tracer_provider(_provider)
    return _collector


def flush() -> None:
    if _provider is not None:
        _provider.force_flush()


def _attr_value(v: Any) -> Any:
    return list(v) if isinstance(v, tuple) else v


def span_to_dict(span: ReadableSpan) -> dict[str, Any]:
    ctx = span.get_span_context()
    parent = span.parent
    return {
        "traceId": format(ctx.trace_id, "032x"),
        "spanId": format(ctx.span_id, "016x"),
        "parentSpanId": format(parent.span_id, "016x") if parent else "",
        "name": span.name,
        "kind": f"SPAN_KIND_{span.kind.name}",
        "startTimeUnixNano": span.start_time,
        "endTimeUnixNano": span.end_time,
        "attributes": {k: _attr_value(v) for k, v in (span.attributes or {}).items()},
        "status": {
            "code": f"STATUS_CODE_{span.status.status_code.name}",
            "message": span.status.description or "",
        },
        "events": [
            {"name": e.name, "timeUnixNano": e.timestamp, "attributes": dict(e.attributes or {})}
            for e in span.events
        ],
        "scope": {"name": span.instrumentation_scope.name, "version": span.instrumentation_scope.version}
        if span.instrumentation_scope
        else {},
        "resource": dict(span.resource.attributes) if span.resource else {},
    }
