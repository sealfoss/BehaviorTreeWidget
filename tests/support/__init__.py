"""Helpers for the test modules (these are not test modules themselves)."""


def regression_node_types() -> list[type]:
    """The leaf node types of the tests that came from the regression rounds; the ``widget``
    fixture registers all of them (each has a type name of its own)."""
    from . import round4, round5, round6, round7, round8, round9

    return [
        round4.Quick, round4.GatedWorker, round4.Writer,
        round5.Quick, round5.Endless, round5.Emitter,
        round6.Endless, round6.Quick, round6.StopIssuer,
        *round7.TYPES,
        *round8.TYPES,
        round9.Quick,
    ]
