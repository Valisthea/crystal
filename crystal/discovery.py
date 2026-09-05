"""Source discovery with automatic language detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .parsers.base import LANGUAGE_BY_SUFFIX, SUPPORTED_SUFFIXES
from .paths import rglob_files

SKIP = {
    ".git", "node_modules", "lib", "cache", "out", "artifacts", ".venv",
    "target", "__pycache__", "broadcast", "typechain", "typechain-types",
    "dist", ".build", "forge-cache", "venv", "site-packages",
}

# Manifest markers that identify a Rust ecosystem without compiling anything.
RUST_FRAMEWORKS = {
    "frame-support": "substrate",
    "frame-system": "substrate",
    "sp-runtime": "substrate",
    "anchor-lang": "anchor",
    "solana-program": "solana",
    "ink": "ink",
    "cosmwasm-std": "cosmwasm",
    "near-sdk": "near",
}


@dataclass
class ProjectProfile:
    root: str
    languages: dict[str, int] = field(default_factory=dict)
    frameworks: list[str] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    skipped_directories: list[str] = field(default_factory=list)


def _keep(path: Path) -> bool:
    return not any(part in SKIP for part in path.parts)


def discover(project, languages=None) -> list[Path]:
    """Return every supported source file under `project`.

    The return type stays `list[Path]` so v1 callers keep working; language
    routing happens in `crystal.parsers.parse_sources`.
    """
    root = Path(project).resolve()
    wanted = set(languages) if languages else None
    found: list[Path] = []
    for suffix in SUPPORTED_SUFFIXES:
        if wanted and LANGUAGE_BY_SUFFIX[suffix] not in wanted:
            continue
        found.extend(p for p in rglob_files(root, f"*{suffix}") if _keep(p))
    return sorted(found)


def profile(project, sources=None) -> ProjectProfile:
    """Describe the target: language mix, frameworks, manifests."""
    root = Path(project).resolve()
    sources = list(sources) if sources is not None else discover(root)

    languages: dict[str, int] = {}
    for path in sources:
        language = LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
        if language:
            languages[language] = languages.get(language, 0) + 1

    frameworks: set[str] = set()
    manifests: list[str] = []
    for manifest in list(rglob_files(root, "Cargo.toml"))[:200]:
        if not _keep(manifest):
            continue
        manifests.append(str(manifest))
        text = manifest.read_text(encoding="utf-8", errors="ignore")
        for crate, framework in RUST_FRAMEWORKS.items():
            if crate in text:
                frameworks.add(framework)
    for marker, framework in (
        ("foundry.toml", "foundry"),
        ("hardhat.config.js", "hardhat"),
        ("hardhat.config.ts", "hardhat"),
        ("Move.toml", "move"),
        ("Anchor.toml", "anchor"),
    ):
        candidate = root / marker
        if candidate.exists():
            frameworks.add(framework)
            manifests.append(str(candidate))

    return ProjectProfile(
        str(root), dict(sorted(languages.items())), sorted(frameworks),
        sorted(manifests)[:50], sorted(SKIP),
    )
