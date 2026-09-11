"""What an analysed target's tooling is allowed to see.

Crystal launches Foundry, Medusa, Halmos and Echidna on contracts it did not
write. Those tools hand the contract's own harness a way back out: Foundry's
`vm.envUint("PRIVATE_KEY")` and `vm.envString(...)` cheatcodes read the process
environment from inside a Solidity test.

Until Build 017 every one of those processes inherited the operator's complete
environment. A deploy key, an RPC URL with an API token, a scanner key, a cloud
credential — all of it reachable from a harness compiled out of the target.

This package owns the answer to one question: what does a child process get?
"""

from .environment import (
    ALWAYS,
    OPT_IN_VARIABLE,
    TOOLCHAIN,
    child_environment,
    isolation_report,
    withheld_names,
)

__all__ = [
    "ALWAYS",
    "OPT_IN_VARIABLE",
    "TOOLCHAIN",
    "child_environment",
    "isolation_report",
    "withheld_names",
]
