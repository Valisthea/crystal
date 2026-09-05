"""The deployment fixture: what a harness needs and cannot derive.

Crystal compiles the *property* — the assertion, the state it reads, the holder
tracking, the handler layer over the entry points, the vacuity gate. It does
not invent how a protocol is deployed (upgradeable proxies, initialiser
arguments that must satisfy the initialiser's own guards, role wiring), who
the actors are, or how to build a call whose arguments the engine cannot
fabricate (an EIP-712 signature by a private key, a Bitcoin transaction that
must parse). Those come from a `Fixture`, written once per target and reused
by every backend and every property. Its cost is the human cost of the path,
and it is reported as such.

A `Recipe` is the one place target-specific knowledge enters a handler. It
receives a fuzz seed and returns a `CrystalCall`: who calls, with how much
ether, with which calldata, and which key (a quote hash, an id) the call is
about. The engine does everything else — pranking, recording every address
handed to the contract, counting successes and reverts, classifying revert
selectors, and evaluating the property.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Instance:
    """A deployed contract the harness talks to through `handle`."""

    contract: str
    handle: str
    path: str  # source path, relative to the project root (or a remapped path)
    proxy: str | None = None
    proxy_path: str | None = None
    proxy_args: str = "address($IMPL), $INIT"  # $IMPL/$INIT placeholders
    init: str | None = None
    init_args: tuple[str, ...] = ()
    ctor_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class Actor:
    """An externally owned account with a known private key."""

    name: str
    key: int
    funding: str = "1000000 ether"


@dataclass(frozen=True)
class SetupCall:
    """One statement run after deployment. When `caller` is set the statement
    is pranked, so it must contain exactly one external call (a nested call —
    `x.grantRole(x.ROLE(), a)` — would consume the prank on the getter)."""

    code: str
    caller: str = ""


@dataclass(frozen=True)
class Recipe:
    """How the harness performs one entry point it cannot synthesise.

    `body` is the body of `function _build_N(uint256 seed, <params>) internal
    returns (CrystalCall memory c)`. `c.target` is preset to the instance;
    the body sets `c.caller`, `c.value`, `c.data`, `c.key`, `c.touched`
    (every address it hands to the contract) and `c.skip` when it cannot act.
    `feeds` names the contract-qualified mapping the returned key belongs to
    (`PegOutContract::_pegOutQuotes`), so other handlers can pick from it.
    """

    function: str  # qualified entry point, e.g. "PegOutContract.depositPegOut"
    body: str
    params: tuple[str, ...] = ()
    feeds: str = ""
    note: str = ""


@dataclass
class Fixture:
    project_root: str
    instances: list[Instance] = field(default_factory=list)
    actors: list[Actor] = field(default_factory=list)
    setup: list[SetupCall] = field(default_factory=list)
    recipes: dict[str, Recipe] = field(default_factory=dict)
    # Verbatim Solidity members (storage, helpers) the recipes rely on.
    members: str = ""
    # Extra imports the members and recipes need: symbol -> path (relative to
    # the project root, or a remapped path). Merged with the generated ones.
    imports: dict[str, str] = field(default_factory=dict)
    # Backend-specific statements run at the very start of setUp.
    prelude: dict[str, str] = field(default_factory=dict)
    # Entry points never driven (qualified names).
    exclude: set[str] = field(default_factory=set)
    # Extra Solidity files written next to the harness: name -> source.
    support_files: dict[str, str] = field(default_factory=dict)
    harness_dir: str = "test/crystal"
    base_timestamp: int = 1_700_000_000
    base_block: int = 1_000_000
    value_cap: str = "1000 ether"
    name: str = "fixture"

    def instance_for(self, contract: str) -> Instance | None:
        for instance in self.instances:
            if instance.contract == contract:
                return instance
        return None

    def import_path(self, path: str) -> str:
        """An import string for `path` as seen from the harness directory."""
        text = path.replace("\\", "/")
        if text.startswith(("@", "forge-std/", "halmos-cheatcodes/")):
            return text
        root = Path(self.project_root).resolve()
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                text = candidate.resolve().relative_to(root).as_posix()
            except ValueError:
                return candidate.as_posix()
        depth = len(Path(self.harness_dir).parts)
        return "../" * depth + text

    def line_count(self) -> int:
        """Human-written lines this fixture carries, for the cost report."""
        total = len(self.members.splitlines())
        for recipe in self.recipes.values():
            total += len(recipe.body.splitlines())
        for source in self.support_files.values():
            total += len(source.splitlines())
        total += len(self.instances) + len(self.actors) + len(self.setup) + len(self.imports)
        total += sum(len(p.splitlines()) for p in self.prelude.values())
        return total
