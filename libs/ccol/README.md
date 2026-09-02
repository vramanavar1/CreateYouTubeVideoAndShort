# ccol — cross-cutting observability layer

Structured logging, correlation ids, severity mapping, metrics and spans for AI
projects, with optional export to Azure Monitor / Application Insights.

## Why it is shaped this way

- **No required dependencies.** Without a connection string, `ccol` is stdlib
  `logging` and nothing more — and no telemetry package is even imported. That is
  what lets a project depend on it unconditionally and keep an offline test suite
  offline.
- **It never reads `os.environ`.** The consuming project owns environment parsing
  and hands over an `ObservabilityConfig`. This keeps the dependency direction
  one-way and makes the layer testable without touching a real environment.
- **Nothing here may fail the caller.** Export that cannot start logs a warning
  and the application carries on. Instruments degrade to no-ops rather than
  raising, so call sites never branch on whether telemetry is configured.

## Use

```python
from ccol import ObservabilityConfig, configure, use_correlation

obs = configure(
    ObservabilityConfig(
        service_name="myapp",
        environment="prod",
        console_format="json",
        connection_string=...,   # empty => stdout only
    )
)

with use_correlation("job-123"):
    obs.event("work started", stage="ingest")
    obs.counter("myapp.jobs").add(1, {"state": "started"})
    with obs.span("ingest", job="job-123"):
        ...
```

Every log record emitted inside a `use_correlation` block carries
`correlation_id`, so one unit of work's whole lifecycle greps out of a mixed
stream. Bindings nest — an outer request scope is restored when an inner job
scope exits.

## Severity

`Level` is a small closed vocabulary (`debug`/`info`/`warning`/`error`/
`critical`). A project maps its own domain severity onto it, so the mapping is
the only place that has to know about `logging` integers.

## Installing with Azure export

```bash
uv sync --extra azure     # pulls azure-monitor-opentelemetry
```

Then pass a `connection_string`. Logs, traces and metrics all flow through the
Azure Monitor OpenTelemetry Distro, so `extra={...}` on any log call becomes
`customDimensions` with no call-site change.
