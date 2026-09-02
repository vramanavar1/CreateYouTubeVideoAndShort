"""Distribution sinks and the registry that fans out to them."""

from ytshort.sinks.base import Sink
from ytshort.sinks.email_sink import EmailSink
from ytshort.sinks.file_sink import FileSink
from ytshort.sinks.registry import build_sinks

__all__ = ["EmailSink", "FileSink", "Sink", "build_sinks"]
