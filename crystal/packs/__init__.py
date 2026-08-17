"""Protocol invariant packs: pluggable campaign definitions by domain.

Each pack defines campaigns, invariants, state concepts, and dangerous
transitions for a protocol family. The core engine is pack-agnostic; packs
are loaded by the campaign registry at runtime.
"""
