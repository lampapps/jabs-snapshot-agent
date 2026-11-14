"""The console's tabs, in display order."""

from .diagnostics import DiagnosticsPane
from .docs import DocsPane
from .files import FilesPane
from .jobs import JobsPane
from .repository import RepositoryPane
from .service import ServicePane

PANES = [JobsPane, RepositoryPane, FilesPane, DiagnosticsPane, ServicePane, DocsPane]
