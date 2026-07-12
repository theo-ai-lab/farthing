"""OpenTelemetry wiring, exported to Langfuse over OTLP/HTTP.

Every workflow, activity, and model call emits a span; the trace id is written
into the audit ledger entry for each executed refund, so a dollar in the ledger
links straight to the trace that moved it. Langfuse is the backend because it
speaks native OTLP and gives the LLM-specific view (token cost, latency) on top
of the general trace.
"""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_CONFIGURED = False


def configure_tracing(service_name: str) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    endpoint = os.environ.get("FARTHING_OTLP_ENDPOINT", "http://localhost:3000/api/public/otel/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _CONFIGURED = True


def current_trace_id() -> str | None:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx or not ctx.trace_id:
        return None
    return format(ctx.trace_id, "032x")
