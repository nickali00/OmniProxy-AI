"""Opt-in OTLP telemetry with a content-free, explicit attribute allowlist."""

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

from app.config import settings

CURRENT = ContextVar("omniproxy_telemetry", default=None)


class Telemetry:
    def __init__(self, *, trace_exporter=None, metric_reader=None):
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        resource = Resource({"service.name": "omniproxy", "service.version": settings.app_version})
        if trace_exporter is None or metric_reader is None:
            parts = urlsplit(settings.otel_endpoint)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                raise ValueError("Set an explicit OTLP HTTP collector endpoint without URL credentials")
        self.traces = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(settings.otel_sample_rate)), shutdown_on_exit=False)
        exporter = trace_exporter if trace_exporter is not None else OTLPSpanExporter(endpoint=settings.otel_endpoint.rstrip("/") + "/v1/traces", timeout=3)
        self.traces.add_span_processor(BatchSpanProcessor(exporter, max_queue_size=256, max_export_batch_size=64, schedule_delay_millis=1000))
        reader = metric_reader if metric_reader is not None else PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=settings.otel_endpoint.rstrip("/") + "/v1/metrics", timeout=3), export_interval_millis=60000, export_timeout_millis=3000)
        self.metrics = MeterProvider(resource=resource, metric_readers=[reader], shutdown_on_exit=False)
        self.tracer = self.traces.get_tracer("omniproxy", settings.app_version)
        meter = self.metrics.get_meter("omniproxy", settings.app_version)
        self.duration = meter.create_histogram("gen_ai.client.operation.duration", unit="s", description="Duration of a gateway inference operation")
        self.requests = meter.create_counter("omniproxy.requests", unit="{request}")
        self.tokens = meter.create_counter("omniproxy.estimated_token.usage", unit="{token}")

    @contextmanager
    def inference(self, provider, model):
        from opentelemetry.trace import SpanKind, Status, StatusCode
        attributes = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": provider, "gen_ai.request.model": model}
        started = time.perf_counter()
        with self.tracer.start_as_current_span("chat " + model, kind=SpanKind.CLIENT, attributes=attributes, record_exception=False, set_status_on_exception=False) as span:
            try:
                yield span
            except BaseException as exc:
                span.set_attribute("error.type", "cancelled" if isinstance(exc, asyncio.CancelledError) else getattr(exc, "code", "provider_error"))
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                self.duration.record(time.perf_counter() - started, attributes)

    def account(self, provider, model, prompt, completion, status):
        attributes = {"gen_ai.provider.name": provider, "gen_ai.request.model": model, "http.response.status_code": status, "omniproxy.tokens.estimated": True}
        self.requests.add(1, attributes)
        self.tokens.add(prompt, {**attributes, "gen_ai.token.type": "input"})
        self.tokens.add(completion, {**attributes, "gen_ai.token.type": "output"})

    def shutdown(self):
        self.traces.shutdown()
        self.metrics.shutdown(timeout_millis=3000)


def configure():
    return Telemetry() if settings.otel_enabled else None


def account_usage(provider, model, prompt, completion, status):
    runtime = CURRENT.get()
    if runtime:
        runtime.account(provider, model, prompt, completion, status)


class TelemetryMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        runtime = getattr(scope.get("app", None).state, "telemetry", None) if scope.get("app") else None
        if scope["type"] != "http" or runtime is None or not scope["path"].startswith(("/v1/", "/v1beta/")):
            return await self.app(scope, receive, send)
        from opentelemetry.trace import SpanKind, Status, StatusCode
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )
        # Baggage, query strings, headers, request bodies and resource IDs are
        # deliberately excluded. Only validated W3C trace IDs are propagated.
        parent = {name.decode(): value.decode("ascii", errors="ignore")[:512] for name, value in scope["headers"] if name in {b"traceparent", b"tracestate"}}
        context = TraceContextTextMapPropagator().extract(parent)
        token = CURRENT.set(runtime)
        try:
            with runtime.tracer.start_as_current_span("HTTP request", context=context, kind=SpanKind.SERVER, attributes={"http.request.method": scope["method"]}, record_exception=False, set_status_on_exception=False) as span:
                async def traced_send(message):
                    if message["type"] == "http.response.start":
                        span.set_attribute("http.response.status_code", message["status"])
                        if message["status"] >= 400:
                            span.set_status(Status(StatusCode.ERROR))
                    await send(message)
                try:
                    await self.app(scope, receive, traced_send)
                except BaseException:
                    span.set_status(Status(StatusCode.ERROR))
                    raise
                finally:
                    route = getattr(scope.get("route"), "path", "/unmatched")
                    span.set_attribute("http.route", route)
                    span.update_name(scope["method"] + " " + route)
        finally:
            CURRENT.reset(token)
