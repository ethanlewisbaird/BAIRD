"""Form input helpers — used by slash commands for structured input.

Extracted from the old tui.py when the Ink frontend replaced the Rich TUI.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .theme import OC


class FormParseError(ValueError):
    pass


@dataclass
class FormField:
    name: str
    prompt: str
    default: str | None = None
    required: bool = False
    validator: Callable[[str], str | None] | None = None


def _form_status_table(fields: list[FormField], known: dict[str, str]) -> Table:
    t = Table(show_header=True, header_style="dim", box=None, pad_edge=False)
    t.add_column("field")
    t.add_column("status")
    t.add_column("value", overflow="fold")
    for f in fields:
        v = known.get(f.name)
        if v is not None and v != "":
            status = f"[{OC.success}]set[/{OC.success}]"
            value = str(v)
        elif f.default is not None:
            status = f"[{OC.info}]default[/{OC.info}]"
            value = f.default
        elif f.required:
            status = f"[{OC.error}]missing[/{OC.error}]"
            value = ""
        else:
            status = f"[{OC.textMuted}]optional[/{OC.textMuted}]"
            value = ""
        t.add_row(f.name, status, value)
    return t


def collect_form_values(
    fields: list[FormField],
    known: dict[str, str] | None,
    *,
    input_fn: Callable[[str], str],
    console: Console | None = None,
) -> dict[str, str]:
    known = dict(known or {})
    for k, v in known.items():
        if isinstance(v, str) and v.startswith("--"):
            hint = v[2:].split()[0] if len(v) > 2 else ""
            suffix = f" — did you mean `--{hint} <value>`?" if hint else ""
            raise FormParseError(
                f"value for {k!r} starts with '--' ({v!r}) — "
                f"looks like an unparsed flag{suffix}"
            )
    if console is not None:
        console.print(
            Panel(
                _form_status_table(fields, known), border_style=OC.info, title="form"
            )
        )

    out: dict[str, str] = {}
    for f in fields:
        v = known.get(f.name)
        if v is not None and v != "":
            out[f.name] = v
            continue
        if not f.required:
            if f.default is not None:
                out[f.name] = f.default
            continue
        while True:
            prompt = f.prompt
            if f.default is not None:
                prompt = f"{prompt} [{f.default}]"
            prompt = prompt + ": "
            raw = input_fn(prompt).strip()
            if raw == "" and f.default is not None:
                raw = f.default
            if raw == "":
                if console is not None:
                    console.print(Text(f"{f.name} is required", style=OC.error))
                continue
            if f.validator is not None:
                err = f.validator(raw)
                if err is not None:
                    if console is not None:
                        console.print(Text(err, style=OC.error))
                    continue
            out[f.name] = raw
            break
    return out
