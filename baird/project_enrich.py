"""Project auto-enrichment — probe a project's locations for the obvious
files (README, CLAUDE.md, .git/config, environment.yml, Dockerfile, *.sif,
pyproject.toml) and propose values for the project record's empty fields.

Split as:
- `probe_location(reader, host, path)` — pure data-collection over a
  `RemoteReader` callable. Tests inject a canned-content reader; the
  slash command wraps an executor.
- `propose_enrichment(project, probes)` — pure synthesis from probe
  results to field proposals.

The slash command in `baird/slash.py` is the only UI layer; this module
holds no I/O of its own.

Wire-in: `/project new` auto-calls enrichment at the end (so creation is
one flow), and `/project enrich <id>` re-runs it after a location is
added.
"""

from __future__ import annotations

import configparser
import re
from collections.abc import Callable
from dataclasses import dataclass, field

# A reader takes (host, path) and returns the file's text contents, or None
# if the file doesn't exist or can't be read. Implementations decide what
# "doesn't exist" means (e.g. an executor's `read_file` raising 404).
RemoteReader = Callable[[str, str], str | None]


# ---- Probe ------------------------------------------------------------


@dataclass
class LocationProbe:
    """What we found at one project location."""

    host: str
    path: str
    readme: str | None = None
    claude_md: str | None = None
    git_origin: str | None = None
    conda_env_name: str | None = None
    has_dockerfile: bool = False
    singularity_image: str | None = None  # basename of the first *.sif found
    pyproject_description: str | None = None


# How many leading lines we keep for each text probe — the proposal logic
# only reads the first paragraph or so.
HEAD_LINES = {
    "README.md": 40,
    "CLAUDE.md": 80,
    "pyproject.toml": 30,
}


def _head(text: str | None, n: int) -> str | None:
    if text is None:
        return None
    return "\n".join(text.splitlines()[:n])


def _extract_git_origin(git_config_text: str) -> str | None:
    """Pull `remote.origin.url` out of a `.git/config` body. Tolerates
    missing-section / missing-key without raising — returns None instead."""
    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read_string(git_config_text)
    except configparser.Error:
        return None
    for section in parser.sections():
        if section.strip().lower() == 'remote "origin"':
            url = parser.get(section, "url", fallback=None)
            if url:
                return url.strip()
    return None


_GITHUB_URL = re.compile(
    r"github\.com[:/]+([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$"
)


def github_slug_from_origin(origin_url: str | None) -> str | None:
    """`git@github.com:owner/repo.git` or `https://github.com/owner/repo` →
    `owner/repo`. Returns None for non-github remotes."""
    if not origin_url:
        return None
    m = _GITHUB_URL.search(origin_url)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


def _extract_conda_name(env_yml_text: str) -> str | None:
    """Pull the top-level `name:` from an environment.yml body. Avoids
    pulling YAML in (the project intentionally keeps probe parsing
    dependency-light)."""
    for line in env_yml_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("name:"):
            value = stripped.split(":", 1)[1].strip()
            # Strip surrounding quotes.
            return value.strip("\"'") or None
    return None


_PYPROJECT_DESCRIPTION = re.compile(
    r'^\s*description\s*=\s*["\']([^"\']+)["\']', re.MULTILINE
)


def _extract_pyproject_description(text: str) -> str | None:
    m = _PYPROJECT_DESCRIPTION.search(text)
    return m.group(1).strip() if m else None


def probe_location(reader: RemoteReader, host: str, path: str) -> LocationProbe:
    """Run the standard set of probes against `host:path`. Missing files
    are silently skipped; the returned `LocationProbe` carries None /
    False for anything not found."""

    def _read(rel: str) -> str | None:
        # Compose path; allow `path` to end in `/` or not.
        full = path.rstrip("/") + "/" + rel
        try:
            return reader(host, full)
        except Exception:
            return None

    readme = _head(_read("README.md"), HEAD_LINES["README.md"])
    claude_md = _head(_read("CLAUDE.md"), HEAD_LINES["CLAUDE.md"])

    git_text = _read(".git/config")
    git_origin = _extract_git_origin(git_text) if git_text else None

    env_yml = _read("environment.yml") or _read("environment.yaml")
    conda_name = _extract_conda_name(env_yml) if env_yml else None

    has_dockerfile = _read("Dockerfile") is not None

    # For Singularity, we can't easily list — accept a sentinel via a known
    # location convention. Reader implementations may stash the basename in
    # a path like `<root>/.baird-sif-name` (probe-time hint), or callers can
    # set this field post-hoc. Keep it None unless the reader supports a
    # listing protocol; the slash-command wrapper handles this.
    singularity = None

    pyproj_text = _read("pyproject.toml")
    pyproj_desc = (
        _extract_pyproject_description(_head(pyproj_text, HEAD_LINES["pyproject.toml"]))
        if pyproj_text else None
    )

    return LocationProbe(
        host=host,
        path=path,
        readme=readme,
        claude_md=claude_md,
        git_origin=git_origin,
        conda_env_name=conda_name,
        has_dockerfile=has_dockerfile,
        singularity_image=singularity,
        pyproject_description=pyproj_desc,
    )


# ---- Propose ----------------------------------------------------------


@dataclass
class FieldProposal:
    """One proposed enrichment value. `value=None` means "we looked and
    didn't find anything to propose" — the form layer surfaces this as
    `(none found — leave blank?)` so the user knows it was attempted."""

    field: str
    value: object | None
    source: str  # short provenance string, e.g. "GPU-wrkstn:/data/x/.git/config"


@dataclass
class EnrichmentProposal:
    proposals: list[FieldProposal] = field(default_factory=list)

    def by_field(self) -> dict[str, FieldProposal]:
        return {p.field: p for p in self.proposals}


def _first(values: list[tuple[str, object | None]]) -> tuple[object | None, str | None]:
    for source, val in values:
        if val:
            return val, source
    return None, None


def _context_paragraph(probes: list[LocationProbe]) -> tuple[str | None, str | None]:
    """Compose a terse one-paragraph context from README / CLAUDE.md /
    pyproject description, whichever is present first across locations.
    We don't try to summarise — the user accepts/edits in the form, and
    the first paragraph of a typical README is already a description."""
    for p in probes:
        for label, body in (
            ("README.md", p.readme),
            ("CLAUDE.md", p.claude_md),
            ("pyproject description", p.pyproject_description),
        ):
            if not body:
                continue
            first_para = body.strip().split("\n\n", 1)[0]
            # Trim heading markers and obvious title lines.
            first_para = "\n".join(
                line.lstrip("# ").strip()
                for line in first_para.splitlines()
                if line.strip()
            )
            if first_para:
                return first_para, f"{p.host}:{p.path}/{label}"
    return None, None


def _env_proposal(probes: list[LocationProbe]) -> tuple[dict | None, str | None]:
    """Build an `env: {...}` dict from probe signals across all locations.
    Empty dict → no signals; surfaced to the user as "(none found)"."""
    env: dict[str, object] = {}
    sources: list[str] = []
    for p in probes:
        if p.conda_env_name and "conda" not in env:
            env["conda"] = p.conda_env_name
            sources.append(f"{p.host}:{p.path}/environment.yml")
        if p.has_dockerfile and "docker" not in env:
            env["docker"] = True
            sources.append(f"{p.host}:{p.path}/Dockerfile")
        if p.singularity_image and "singularity" not in env:
            env["singularity"] = p.singularity_image
            sources.append(f"{p.host}:{p.path}/{p.singularity_image}")
    if not env:
        return None, None
    return env, "; ".join(sources)


def propose_enrichment(
    current: dict, probes: list[LocationProbe]
) -> EnrichmentProposal:
    """Look at the current project record + the per-location probe results
    and propose values for the empty fields.

    `current` is the project row as returned by HubClient.get_project /
    upsert_project — at minimum `{id, name, github, context, config}`.

    Only fields the user hasn't already filled in get proposals; pre-set
    fields are left alone (the user picked those on purpose at /project
    new time, or via a previous enrichment).
    """
    proposals: list[FieldProposal] = []

    if not current.get("github"):
        slug, source = _first([
            (
                f"{p.host}:{p.path}/.git/config",
                github_slug_from_origin(p.git_origin),
            )
            for p in probes
        ])
        proposals.append(FieldProposal(
            field="github", value=slug, source=source or "(no .git/config found)"
        ))

    if not current.get("context"):
        ctx_text, source = _context_paragraph(probes)
        proposals.append(FieldProposal(
            field="context", value=ctx_text,
            source=source or "(no README/CLAUDE.md/pyproject description found)",
        ))

    cfg = current.get("config") or {}
    if not cfg.get("env"):
        env_dict, source = _env_proposal(probes)
        proposals.append(FieldProposal(
            field="env", value=env_dict,
            source=source or "(no environment.yml / Dockerfile / .sif found)",
        ))

    return EnrichmentProposal(proposals=proposals)


__all__ = [
    "LocationProbe",
    "FieldProposal",
    "EnrichmentProposal",
    "RemoteReader",
    "probe_location",
    "propose_enrichment",
    "github_slug_from_origin",
]
