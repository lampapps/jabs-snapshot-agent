"""Modal dialogs for the admin console. Text shown here may come from files or agents, so markup is off."""

import asyncio
from typing import Any, Callable, Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from .. import config
from ..restic import ResticError


class _Dialog(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(self.cancel_value())

    def cancel_value(self) -> Any:
        return None


class ConfirmDialog(_Dialog):
    """Yes/no question. Returns (confirmed, option_checked); `type_word` makes the user type a word to enable Yes."""

    def __init__(self, title: str, message: str, confirm: str = "Yes", danger: bool = False,
                 type_word: Optional[str] = None, option: Optional[str] = None):
        super().__init__()
        self._title, self._message, self._confirm = title, message, confirm
        self._danger, self._word, self._option = danger, type_word, option

    def cancel_value(self) -> tuple:
        return (False, False)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._title, classes="dialog-title")
            yield Static(self._message, markup=False, classes="dialog-body")
            if self._word:
                yield Label(f"Type {self._word} to confirm:")
                yield Input(id="word", placeholder=self._word)
            if self._option:
                yield Checkbox(self._option, id="option")
            with Horizontal(classes="buttons"):
                yield Button(self._confirm, id="ok", variant="error" if self._danger else "primary",
                             disabled=bool(self._word))
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#word" if self._word else "#cancel").focus()

    @on(Input.Changed, "#word")
    def _word_changed(self, event: Input.Changed) -> None:
        self.query_one("#ok", Button).disabled = event.value.strip() != self._word

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            option = self.query_one("#option", Checkbox).value if self._option else False
            self.dismiss((True, option))
        else:
            self.dismiss((False, False))


class MessageDialog(_Dialog):
    """Read-only text (output, errors, a key shown once). `copy_text` adds a Copy button."""

    def __init__(self, title: str, body: str, copy_text: Optional[str] = None, error: bool = False):
        super().__init__()
        self._title, self._body, self._copy, self._error = title, body, copy_text, error

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide" + (" error" if self._error else "")):
            yield Label(self._title, classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                yield Static(self._body, markup=False, classes="dialog-body mono")
            with Horizontal(classes="buttons"):
                if self._copy is not None:
                    yield Button("Copy", id="copy")
                yield Button("Close", id="close", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#close").focus()

    @on(Button.Pressed, "#copy")
    def _copy_pressed(self) -> None:
        self.app.copy_to_clipboard(self._copy or "")
        self.notify("Copied to the clipboard (if your terminal allows it)")

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss(None)


class ChoiceDialog(_Dialog):
    """A question with several buttons; returns the chosen value (None on Escape)."""

    def __init__(self, title: str, message: str, choices: list):
        super().__init__()
        self._title, self._message, self._choices = title, message, choices

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(self._title, classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                yield Static(self._message, markup=False, classes="dialog-body")
            with Horizontal(classes="buttons"):
                for index, (label, _value) in enumerate(self._choices):
                    yield Button(label, id=f"choice-{index}", variant="primary" if index == 0 else "default")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self._choices[int(event.button.id.split("-")[1])][1])


class PickDialog(_Dialog):
    """Choose one of several options; returns its value. With `custom_label`, a free-text entry is offered too."""

    def __init__(self, title: str, options: list, custom_label: Optional[str] = None):
        super().__init__()
        self._title, self._options, self._custom = title, options, custom_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(self._title, classes="dialog-title")
            yield OptionList(*[Option(label, id=str(i)) for i, (label, _v) in enumerate(self._options)],
                             markup=False, id="options")
            if self._custom:
                yield Label(self._custom)
                yield Input(id="custom")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        options = self.query_one("#options", OptionList)
        if self._options:
            options.highlighted = 0
        options.focus()

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._options[int(event.option_id)][1])

    @on(Input.Submitted, "#custom")
    def _custom_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.dismiss(event.value.strip())

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class FormDialog(_Dialog):
    """A form. `fields` are dicts: key, label, kind ('text'|'int'|'bool'|'password'), value, help.

    `submit(values)` runs in a thread; a ConfigError/DbError/ValueError it raises is shown in the form, which stays
    open. The dialog returns submit's result, or None when cancelled.
    """

    def __init__(self, title: str, fields: list, submit: Callable[[dict], Any], save_label: str = "Save"):
        super().__init__()
        self._title, self._fields, self._submit, self._save = title, fields, submit, save_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(self._title, classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                for field in self._fields:
                    if field["kind"] == "bool":
                        yield Checkbox(field["label"], bool(field.get("value")), id=f"f-{field['key']}")
                    else:
                        yield Label(field["label"])
                        yield Input(str(field.get("value") if field.get("value") is not None else ""),
                                    password=field["kind"] == "password", id=f"f-{field['key']}")
                    if field.get("help"):
                        yield Static(field["help"], markup=False, classes="help")
            yield Static("", id="error", markup=False, classes="error-text")
            with Horizontal(classes="buttons"):
                yield Button(self._save, id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        first = self.query("Input")
        if first:
            first.first().focus()

    def _values(self) -> dict:
        values = {}
        for field in self._fields:
            widget = self.query_one(f"#f-{field['key']}")
            values[field["key"]] = widget.value if field["kind"] == "bool" else widget.value.strip()
        return values

    async def _save_now(self) -> None:
        error = self.query_one("#error", Static)
        values = self._values()
        for field in self._fields:
            if field["kind"] == "int":
                try:
                    values[field["key"]] = int(values[field["key"]])
                except ValueError:
                    error.update(f"{field['label']} must be a whole number")
                    return
        try:
            result = await asyncio.to_thread(self._submit, values)
        except (config.ConfigError, ResticError, ValueError, OSError) as exc:
            error.update(str(exc))
            return
        self.dismiss(result)

    @on(Input.Submitted)
    async def _submitted(self) -> None:
        await self._save_now()

    @on(Button.Pressed)
    async def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            await self._save_now()
        else:
            self.dismiss(None)
