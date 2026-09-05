"""Runtime topology: which pallets exist, and how the workspace is laid out.

Cross-module composition is only visible from the runtime crate. A pallet read
on its own cannot show that something else in the same pipeline moves value it
does not guard, so Crystal needs to know whether it is looking at a workspace or
at a single crate — and to say so when it is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..parsers.base import is_test_source
from ..paths import rglob_files

# `#[runtime::pallet_index(3)] pub type TransactionPayment = pallet_transaction_payment;`
MODERN_PALLET_RE = re.compile(
    r"#\[runtime::pallet_index\((\d+)\)\]\s*(?:#\[[^\]]*\]\s*)*"
    r"pub\s+type\s+(\w+)\s*=\s*([\w:]+)",
    re.MULTILINE,
)
# `System: frame_system::{Pallet, Call, ..},` inside construct_runtime!
LEGACY_PALLET_RE = re.compile(
    r"(?m)^\s*(?:#\[[^\]]*\]\s*)?([A-Z]\w*)\s*:\s*([a-z_][\w]*)(?:::\{[^}]*\})?\s*(?:=\s*(\d+))?\s*,"
)
RUNTIME_MACRO_RE = re.compile(r"#\[frame_support::runtime\]|construct_runtime\s*!")

WORKSPACE_RE = re.compile(r"(?m)^\s*\[workspace\]")
FRAMEWORK_RE = re.compile(r"frame-support|frame-system|sp-runtime|polkadot-sdk")
# A target can be Substrate without a manifest in the scan (a copied `src/`
# tree), and the warning must still fire for it.
SUBSTRATE_SOURCE_RE = re.compile(
    r"#\[pallet::|frame_support::|frame_system::|sp_runtime::|TransactionExtension"
)


@dataclass(frozen=True)
class RuntimeModule:
    """One pallet as the runtime declares it."""

    alias: str
    crate: str
    index: int | None = None
    path: str = ""
    line: int = 0


@dataclass
class WorkspaceProfile:
    root: str
    is_workspace: bool = False
    has_runtime: bool = False
    runtime_files: list[str] = field(default_factory=list)
    crate_manifests: list[str] = field(default_factory=list)
    substrate: bool = False

    @property
    def single_crate(self) -> bool:
        return not self.is_workspace and len(self.crate_manifests) <= 1

    def warning(self) -> str:
        """The message a reviewer needs when composition cannot be computed."""
        if self.has_runtime:
            return ""
        if not self.substrate:
            return ""
        return (
            "scanning without a runtime crate. Cross-module composition requires "
            "the runtime (with construct_runtime! / #[frame_support::runtime] and "
            "the TxExtension tuple). Run `crystal scan <workspace_root>` for full "
            "pipeline analysis; intra-module signals are unaffected."
        )


@dataclass
class RuntimeTopology:
    modules: list[RuntimeModule] = field(default_factory=list)
    profile: WorkspaceProfile | None = None

    @property
    def by_alias(self) -> dict[str, RuntimeModule]:
        return {module.alias: module for module in self.modules}

    @property
    def by_crate(self) -> dict[str, RuntimeModule]:
        return {module.crate: module for module in self.modules}

    def crate_of(self, reference: str) -> str:
        """`pallet_transaction_payment::ChargeTransactionPayment` -> the crate."""
        head = re.sub(r"<[^>]*>", "", reference or "").strip().split("::")[0]
        if head in self.by_crate:
            return head
        module = self.by_alias.get(head)
        return module.crate if module else head


def profile_workspace(root, sources=()) -> WorkspaceProfile:
    root_path = Path(root).resolve()
    profile = WorkspaceProfile(str(root_path))

    manifests = list(rglob_files(
        root_path, "Cargo.toml",
        frozenset({"target", ".git", "node_modules"}),
    ))[:400]
    profile.crate_manifests = [str(path) for path in manifests]
    for manifest in manifests:
        text = manifest.read_text(encoding="utf-8", errors="ignore")
        if WORKSPACE_RE.search(text):
            profile.is_workspace = True
        if FRAMEWORK_RE.search(text):
            profile.substrate = True

    for path in sources or ():
        if Path(path).suffix.lower() != ".rs":
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if SUBSTRATE_SOURCE_RE.search(text):
            profile.substrate = True
        # A mock runtime in `mock.rs` is a fixture, not the system under test.
        # Counting it as "the runtime was found" suppresses the warning that
        # tells the reviewer composition analysis is not actually running.
        if RUNTIME_MACRO_RE.search(text) and not is_test_source(path):
            profile.has_runtime = True
            profile.runtime_files.append(str(path))
    return profile


def extract_modules(sources=()) -> list[RuntimeModule]:
    """Pallet declarations, in either runtime macro format.

    The modern `#[frame_support::runtime]` form is what current Substrate emits;
    `construct_runtime!` is still everywhere in older trees. Supporting only one
    means the topology is empty on half of real targets.
    """
    modules: list[RuntimeModule] = []
    seen: set[tuple[str, str]] = set()

    for path in sources or ():
        source = Path(path)
        if source.suffix.lower() != ".rs":
            continue
        # `mock.rs` declares a test runtime with its own pallet indices. Mixing
        # it into the topology gives two pallets per index and resolves aliases
        # to fixtures.
        if is_test_source(path):
            continue
        try:
            text = source.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not RUNTIME_MACRO_RE.search(text):
            continue

        for match in MODERN_PALLET_RE.finditer(text):
            index, alias, crate = int(match.group(1)), match.group(2), match.group(3)
            key = (alias, crate)
            if key in seen:
                continue
            seen.add(key)
            modules.append(RuntimeModule(
                alias, crate.split("::")[0], index, str(source),
                text.count("\n", 0, match.start()) + 1,
            ))

        if not any(m.path == str(source) for m in modules):
            for block in _construct_runtime_blocks(text):
                for match in LEGACY_PALLET_RE.finditer(block):
                    alias, crate = match.group(1), match.group(2)
                    if alias in {"Block", "NodeBlock", "UncheckedExtrinsic"}:
                        continue
                    key = (alias, crate)
                    if key in seen:
                        continue
                    seen.add(key)
                    modules.append(RuntimeModule(
                        alias, crate,
                        int(match.group(3)) if match.group(3) else None,
                        str(source),
                        text.count("\n", 0, text.find(block)) + 1,
                    ))

    return sorted(modules, key=lambda m: (m.index if m.index is not None else 999,
                                          m.alias))


def _construct_runtime_blocks(text: str):
    for match in re.finditer(r"construct_runtime\s*!\s*[({]", text):
        start = match.end() - 1
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char in "({":
                depth += 1
            elif char in ")}":
                depth -= 1
                if depth == 0:
                    yield text[start:index + 1]
                    break


def build_topology(root, sources=()) -> RuntimeTopology:
    return RuntimeTopology(
        modules=extract_modules(sources),
        profile=profile_workspace(root, sources),
    )
