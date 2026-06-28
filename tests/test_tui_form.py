"""Tests for the form-collection primitive in tui.py (Slice B)."""

from __future__ import annotations

from baird.tui import FormField, collect_form_values


def _stub_input(answers: list[str]):
    it = iter(answers)

    def _read(_prompt: str) -> str:
        return next(it)

    return _read


def test_skips_already_known_fields() -> None:
    fields = [
        FormField("host", "Host", required=True),
        FormField("path", "Path", required=True),
    ]
    out = collect_form_values(
        fields,
        {"host": "workstation", "path": "/data"},
        input_fn=_stub_input([]),  # no prompts expected
    )
    assert out == {"host": "workstation", "path": "/data"}


def test_prompts_for_missing_required_only() -> None:
    fields = [
        FormField("host", "Host", required=True),
        FormField("path", "Path", required=True),
        FormField("role", "Role", required=False),
    ]
    out = collect_form_values(
        fields,
        {"host": "workstation"},
        input_fn=_stub_input(["/data"]),
    )
    assert out == {"host": "workstation", "path": "/data"}
    assert "role" not in out  # optional, not asked


def test_default_used_when_blank_input() -> None:
    fields = [FormField("ref", "Ref", default="main", required=True)]
    out = collect_form_values(fields, {}, input_fn=_stub_input([""]))
    assert out == {"ref": "main"}


def test_default_used_for_optional_without_prompt() -> None:
    fields = [FormField("ref", "Ref", default="main", required=False)]
    # No required fields → no prompt; default flows through.
    out = collect_form_values(fields, {}, input_fn=_stub_input([]))
    assert out == {"ref": "main"}


def test_validator_reprompts_on_error() -> None:
    def must_start_with_slash(v: str) -> str | None:
        return None if v.startswith("/") else "must be an absolute path"

    fields = [FormField("path", "Path", required=True, validator=must_start_with_slash)]
    out = collect_form_values(
        fields,
        {},
        input_fn=_stub_input(["relative", "/abs"]),
    )
    assert out == {"path": "/abs"}


def test_required_reprompts_on_empty() -> None:
    fields = [FormField("x", "X", required=True)]
    out = collect_form_values(fields, {}, input_fn=_stub_input(["", "  ", "ok"]))
    assert out == {"x": "ok"}


def test_known_empty_string_treated_as_missing() -> None:
    fields = [FormField("x", "X", required=True)]
    out = collect_form_values(fields, {"x": ""}, input_fn=_stub_input(["here"]))
    assert out == {"x": "here"}
