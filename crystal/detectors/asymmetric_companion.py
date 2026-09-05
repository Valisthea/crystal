"""Missing-companion asymmetry: most equivalent paths do X, this one does not.

Split out of `asymmetric-side-effect` because the two shapes were being scored
by different evidence and rendered on one scale, with nothing establishing that
a number from one was comparable to a number from the other.

The guard shape is graded by *coupling*: whether the differentiating condition
speaks about the state the shared callee writes and the arguments it consumes.
That grading is calibrated against three evaluated cases on three protocols.

This shape has no such grading available. Its argument is inductive — N-1 of N
equivalent sites include a companion, so the one that does not is worth a look
— and the natural analogue of coupling, "does the absent companion touch the
state the primary operation writes", is usually unanswerable: the primary
operation is an inherited `_burn`, an interface method with no body, or an
ERC-20 outside the scanned tree, so Crystal cannot see what it writes. Measured
across two protocols, the primary operation resolved to something with a known
effect in a small minority of cases.

So the confidence here is what it has always been — the proportion of
equivalent sites that include the companion — and it is named as such rather
than dressed as a defect confidence. Where the coupling *can* be established it
is reported, and only then may the signal leave the low band.
"""

from __future__ import annotations

from .asymmetric_side_effect import COMPANION_DETECTOR as DETECTOR
from .asymmetric_side_effect import detect_companion as detect

__all__ = ["DETECTOR", "detect"]
