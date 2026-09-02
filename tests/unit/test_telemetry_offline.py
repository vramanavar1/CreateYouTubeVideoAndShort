"""Proof that an unconfigured install exports nothing and dials nowhere.

This is the test that lets the rest of the suite stay honest: if configuring
observability could reach the network, every other test in the repo would be one
misconfigured environment variable away from shipping data to a real workspace.
"""

from __future__ import annotations

import sys

import pytest
from ccol import ObservabilityConfig, configure, current
from ccol import observability as observability_module
from ccol.metrics import NoopInstrument

from ytshort.config import Settings
from ytshort.observability.setup import configure_observability, observability_config

_TELEMETRY_PACKAGES = ("opentelemetry", "azure.monitor")


def _telemetry_modules() -> set[str]:
    return {
        name
        for name in sys.modules
        if any(name == pkg or name.startswith(f"{pkg}.") for pkg in _TELEMETRY_PACKAGES)
    }


class TestUnconfigured:
    def test_no_telemetry_package_is_imported(self) -> None:
        # The strongest available proof of "no network": you cannot call an
        # exporter you never imported. It holds because ccol._azure is the sole
        # import boundary and every import inside it is lazy.
        before = _telemetry_modules()
        obs = configure(ObservabilityConfig(service_name="test"))

        assert obs.azure_enabled is False
        assert _telemetry_modules() == before

    def test_instruments_are_no_ops_that_never_raise(self) -> None:
        obs = configure(ObservabilityConfig(service_name="test"))

        counter = obs.counter("test.counter")
        histogram = obs.histogram("test.histogram")
        assert isinstance(counter, NoopInstrument)
        assert isinstance(histogram, NoopInstrument)

        counter.add(1, {"any": "attribute"})
        histogram.record(12.5)

    def test_a_span_still_runs_its_block(self) -> None:
        obs = configure(ObservabilityConfig(service_name="test"))
        ran = False
        with obs.span("work", job_id="j1") as span:
            ran = True
        assert ran
        assert span is None

    def test_instruments_are_cached_per_name(self) -> None:
        obs = configure(ObservabilityConfig(service_name="test"))
        assert obs.counter("test.same") is obs.counter("test.same")

    def test_current_is_usable_before_configure(self) -> None:
        observability_module._current = None
        assert current().azure_enabled is False
        current().counter("anything").add(1)


class TestConfiguredWithoutTheExtra:
    def test_it_degrades_to_stdout_instead_of_raising(self, monkeypatch) -> None:
        """A connection string set, but azure-monitor-opentelemetry not installed.

        This is a realistic deployment slip -- the Bicep sets the env var, the
        image was built without the extra. It must not take the pipeline down.
        """
        called: list[str] = []

        def refuse(_name, *_args, **_kwargs):
            called.append(_name)
            raise ImportError("azure-monitor-opentelemetry is not installed")

        import builtins

        real_import = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name.startswith("azure.monitor"):
                return refuse(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guarded)

        obs = configure(
            ObservabilityConfig(
                service_name="test",
                connection_string="InstrumentationKey=00000000-0000-0000-0000-000000000000",
            )
        )

        assert called, "the azure import should have been attempted"
        assert obs.azure_enabled is False
        obs.counter("test.counter").add(1)


class TestSettingsIntegration:
    def test_an_empty_connection_string_leaves_telemetry_off(self, tmp_path) -> None:
        settings = Settings.load(env_file=tmp_path / "absent.env", strict=False)
        assert settings.telemetry_configured is False
        assert configure_observability(settings).azure_enabled is False

    def test_telemetry_is_off_when_disabled_even_with_a_connection_string(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=x")
        monkeypatch.setenv("YTSHORT_TELEMETRY_ENABLED", "false")
        settings = Settings.load(env_file=tmp_path / "absent.env", strict=False)
        assert settings.telemetry_configured is False

    def test_the_config_carries_the_service_identity(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("YTSHORT_SERVICE_NAME", "ytshort-job")
        monkeypatch.setenv("YTSHORT_ENVIRONMENT", "dev")
        settings = Settings.load(env_file=tmp_path / "absent.env", strict=False)

        config = observability_config(settings)
        assert config.service_name == "ytshort-job"
        assert config.environment == "dev"
        # Carried across so the shim's behaviour is unchanged from before ccol.
        assert "googleapiclient" in config.noisy_loggers


@pytest.fixture(autouse=True)
def _reset_observability():
    yield
    observability_module._current = None
