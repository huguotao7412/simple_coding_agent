from __future__ import annotations

from enum import StrEnum


class FinalizationStep(StrEnum):
    VALIDATE_ARTIFACTS = "validate_artifacts"
    PERSIST_VERIFICATION = "persist_verification"
    COMMIT_GRAPH_CURSOR = "commit_graph_cursor"
    TRANSITION_RUN_TERMINAL = "transition_run_terminal"


FINALIZATION_ORDER = tuple(FinalizationStep)


__all__ = ["FINALIZATION_ORDER", "FinalizationStep"]
