"""Correlation ids and formatters -- the parts every other log line depends on."""

from __future__ import annotations

import json
import logging

from ccol import UNSET, correlation_id, new_correlation_id
from ccol.logging import ConsoleFormatter, JsonFormatter, setup_logging

from ytshort.observability.logging import use_job


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "ytshort.stages.ingest", logging.INFO, __file__, 10, "hello", None, None
    )
    record.correlation_id = correlation_id()
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestCorrelationId:
    def test_it_is_on_every_record_inside_the_scope(self) -> None:
        with use_job("abc123"):
            assert json.loads(JsonFormatter().format(_record()))["correlation_id"] == "abc123"

    def test_it_is_unset_outside_any_scope(self) -> None:
        assert correlation_id() == UNSET
        assert json.loads(JsonFormatter().format(_record()))["correlation_id"] == UNSET

    def test_nesting_restores_the_outer_id(self) -> None:
        # The CLI binds a run id; the runner binds a job id per job inside it. The
        # run id has to survive each job, or a multi-job run loses its thread.
        with use_job("run-1"):
            with use_job("job-1"):
                assert correlation_id() == "job-1"
            assert correlation_id() == "run-1"
        assert correlation_id() == UNSET

    def test_new_ids_are_unique_and_safe_for_a_log_field(self) -> None:
        ids = {new_correlation_id() for _ in range(100)}
        assert len(ids) == 100
        assert all(value.isalnum() and len(value) == 32 for value in ids)


class TestFormatters:
    def test_json_promotes_extras_to_top_level_keys(self) -> None:
        payload = json.loads(JsonFormatter().format(_record(stage="ingest", ms=12)))
        assert payload["stage"] == "ingest"
        assert payload["ms"] == 12
        assert payload["message"] == "hello"
        assert payload["level"] == "INFO"

    def test_console_trims_the_configured_prefix_and_shortens_the_id(self) -> None:
        with use_job("0123456789abcdef"):
            line = ConsoleFormatter("ytshort").format(_record())
        assert "stages.ingest" in line
        assert "ytshort.stages.ingest" not in line
        assert "[01234567]" in line

    def test_console_keeps_the_full_name_when_no_prefix_is_configured(self) -> None:
        assert "ytshort.stages.ingest" in ConsoleFormatter().format(_record())


class TestSetupLogging:
    def test_it_is_idempotent(self) -> None:
        # bootstrap() may run more than once in a process; stacking handlers would
        # duplicate every line.
        setup_logging(level="INFO", fmt="json")
        setup_logging(level="INFO", fmt="json")
        assert len(logging.getLogger().handlers) == 1

    def test_it_writes_a_json_file_when_asked(self, tmp_path) -> None:
        log_file = tmp_path / "nested" / "ytshort.jsonl"
        setup_logging(level="INFO", fmt="console", log_file=log_file)
        with use_job("job-9"):
            logging.getLogger("ytshort.test").info("written", extra={"stage": "ingest"})

        for handler in logging.getLogger().handlers:
            handler.flush()
        payload = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
        assert payload["correlation_id"] == "job-9"
        assert payload["stage"] == "ingest"

    def test_it_quiets_the_loggers_it_is_given(self) -> None:
        setup_logging(level="DEBUG", fmt="json", noisy_loggers=("some.chatty.client",))
        assert logging.getLogger("some.chatty.client").level == logging.WARNING
