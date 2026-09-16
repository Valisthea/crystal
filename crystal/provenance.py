"""Who produced a result, from what, under which configuration.

Two rules, and both are refusals.

**Nothing is inferred from the current tree.** A record that lacks provenance
keeps lacking it. Filling the gap with today's revision, today's config or
today's tool version would produce a record that looks auditable and answers a
question nobody asked — what the tree looks like now, rather than what it
looked like when the analysis ran.

**Nothing here is a clock.** Provenance is content-addressed: the same sources
under the same configuration produce the same digests on any machine, on any
day. A timestamp would make every run differ and turn determinism checks into
noise. Where a caller genuinely needs a wall-clock time it belongs beside this
record, not inside it.

Paths are digested relative to the project root, because an absolute path is a
fact about a laptop rather than about the code.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import __build__, __version__

PROVENANCE_SCHEMA = "crystal-provenance/1.0"

# What a digest says when the file could not be read. Kept distinct from any
# real digest so a reader can tell "unreadable" from "empty".
UNREADABLE = "unreadable"


def _digest(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _relative(path, root) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        # Outside the project, or unresolvable. The basename is all that can be
        # said without leaking the operator's directory layout.
        return Path(path).name


def source_digest(sources, root) -> tuple[str, int]:
    """A content digest over the analysed sources, and how many there were.

    Content, not modification time: a file touched but unchanged must not
    change the digest, or every provenance comparison becomes meaningless.
    """
    entries = []
    for path in sorted(sources or (), key=lambda item: str(item)):
        try:
            body = Path(path).read_bytes()
            digest = hashlib.sha256(body).hexdigest()
        except OSError:
            digest = UNREADABLE
        entries.append(f"{_relative(path, root)}:{digest}")
    return _digest("\n".join(entries)), len(entries)


def configuration_digest(configuration: dict) -> str:
    """A digest of the settings that actually shaped this run."""
    material = "\n".join(
        f"{key}={configuration[key]!r}" for key in sorted(configuration)
    )
    return _digest(material)


def producer_provenance(*, project, sources, configuration, parsers=None,
                        tools=None) -> dict:
    """The provenance stamp carried by everything this run produces.

    `tools` is the versions of external tools that were actually probed. A tool
    that was never probed is absent from the mapping rather than recorded as
    unavailable — those are different claims, and only the first is true.
    """
    digest, counted = source_digest(sources, project)
    return {
        "schema": PROVENANCE_SCHEMA,
        "producer": "crystal",
        "producer_version": __version__,
        "producer_build": __build__,
        "source_digest": digest,
        "source_count": counted,
        "configuration": dict(sorted(configuration.items())),
        "configuration_digest": configuration_digest(configuration),
        "parsers": dict(sorted((parsers or {}).items())),
        "tool_versions": dict(sorted((tools or {}).items())),
    }


def merge_missing(record: dict, stamp: dict) -> dict:
    """Fill only the keys a record does not have, and say which were filled.

    For legacy records read back from disk. A field the record already carries
    is never overwritten with today's value — that is the whole point — and the
    keys that were absent are listed rather than quietly completed, so a reader
    can tell an original stamp from a repaired one.
    """
    if not record:
        return dict(stamp, provenance_origin="stamped-at-read", filled=sorted(stamp))
    filled = [key for key in stamp if key not in record]
    if not filled:
        return dict(record)
    merged = dict(record)
    for key in filled:
        merged[key] = stamp[key]
    merged["provenance_origin"] = "partially-stamped-at-read"
    merged["filled"] = sorted(filled)
    return merged
