"""Docs tab: read the Markdown files of the install (README, AGENTS, ...) inside the console."""

import asyncio
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import MarkdownViewer, OptionList, Static
from textual.widgets.option_list import Option

from ... import settings
from .. import docs
from ..app import Pane


class DocViewer(MarkdownViewer):
    """A viewer that reports link clicks instead of opening them, so web links never launch a browser."""

    class LinkRequested(Message):
        def __init__(self, href: str) -> None:
            super().__init__()
            self.href = href

    async def on_markdown_link_clicked(self, message) -> None:
        message.prevent_default()
        message.stop()
        self.post_message(self.LinkRequested(message.href))


class DocsPane(Pane):
    TAB_ID = "docs"
    TAB_TITLE = "6 Docs"
    ACTIONS: dict = {}
    BINDINGS = [Binding("f", "focus_files", "File list"), Binding("b", "back", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self._files: list = []
        self._current: Optional[Path] = None
        self._history: list = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="docs-layout"):
            with Vertical(id="docs-side"):
                yield Static("", id="docs-note", markup=False, classes="note")
                yield OptionList(id="docs-files", markup=False)
            yield DocViewer(show_table_of_contents=False, open_links=False, id="docs-viewer")

    def focus_primary(self) -> None:
        self.query_one("#docs-files", OptionList).focus()

    async def reload(self) -> None:
        self._files = await asyncio.to_thread(docs.find_markdown, settings.BASE_DIR)
        options = self.query_one("#docs-files", OptionList)
        options.clear_options()
        options.add_options([Option(str(path), id=str(i)) for i, path in enumerate(self._files)])
        self.query_one("#docs-note", Static).update(
            f"{len(self._files)} Markdown file(s) under {settings.BASE_DIR}" if self._files
            else f"No Markdown files found under {settings.BASE_DIR}")
        if self._files and self._current is None:
            options.highlighted = 0
            await self.open_doc(self._files[0])

    async def open_doc(self, relative: object, remember: bool = True) -> None:
        try:
            path, text = await asyncio.to_thread(docs.read_doc, settings.BASE_DIR, relative)
        except docs.DocError as exc:
            self.notify(str(exc), severity="error")
            return
        if remember and self._current is not None and self._current != path:
            self._history.append(self._current)
        self._current = path
        viewer = self.query_one("#docs-viewer", DocViewer)
        await viewer.document.update(text)
        viewer.document.scroll_home(animate=False)
        viewer.border_title = str(path.relative_to(Path(settings.BASE_DIR).resolve()))

    @on(OptionList.OptionSelected, "#docs-files")
    def _file_selected(self, event: OptionList.OptionSelected) -> None:
        self.run_worker(self.open_doc(self._files[int(event.option_id)]), group="docs", exclusive=True)

    @on(DocViewer.LinkRequested)
    def _link(self, event: DocViewer.LinkRequested) -> None:
        self.run_worker(self._follow(event.href), group="docs", exclusive=True)

    async def _follow(self, href: str) -> None:
        if href.startswith("#"):
            await self.query_one("#docs-viewer", DocViewer).go(href)
            return
        if urlparse(href).scheme:
            self.notify(f"External link, not opened: {href}")
            return
        target = docs.link_target(settings.BASE_DIR, self._current, href)
        if target is None:
            self.notify(f"{href} is not a document in this install", severity="warning")
            return
        await self.open_doc(target.relative_to(Path(settings.BASE_DIR).resolve()))

    def action_focus_files(self) -> None:
        self.focus_primary()

    def action_back(self) -> None:
        if not self._history:
            return
        previous = self._history.pop()
        self.run_worker(self.open_doc(previous.relative_to(Path(settings.BASE_DIR).resolve()), remember=False),
                        group="docs", exclusive=True)
