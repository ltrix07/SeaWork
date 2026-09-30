"""The only place where numbers that shape a score live.

EVERYTHING HERE IS A PLACEHOLDER. The contract (6.1, 6.2) says the factor weights and
the confidence-to-weight function are undecided, and that nothing exists to measure
them against: there is not one "profile x vacancy" pair rated by a human. Task E2.1
(a hand-labelled gold set of 30-50 pairs) is what will let these be calibrated. Until
then the values below are deliberately dull.

Equal weights are the honest choice. A tidy hierarchy ("profession counts double")
would look like knowledge nobody has, and a later reader would treat it as measured.
Do not tune these to make examples look plausible; that is exactly the false
precision the contract refuses.
"""

from seawork.domain.match import FactorOutcome

# One weight for every factor. Deliberately not a per-factor table: adding a table
# would invite filling it in with guesses.
BASE_WEIGHT = 1.0


def factor_weight(
    outcome: FactorOutcome,
    opportunity_confidence: float | None,
    profile_confidence: float | None,
) -> float:
    """Weight of one verdict; confidence enters the weight (contract 3.5).

    PROVISIONAL form: the product of the two confidences, a missing one counting as
    1.0 (a value read straight from a source or a person is not a guess). Simplest
    function that satisfies the principle that a 0.6 inference cannot weigh as much
    as a stated answer; there is no evidence yet that a product beats a minimum, a
    square, or a threshold. See contract 6.2.
    """
    if outcome is FactorOutcome.NOT_COMPARABLE:
        return 0.0
    opportunity = 1.0 if opportunity_confidence is None else opportunity_confidence
    profile = 1.0 if profile_confidence is None else profile_confidence
    return BASE_WEIGHT * opportunity * profile
