from __future__ import annotations

from .models import Capability


TOOL_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "list_dir": frozenset({Capability.READ_WORKSPACE}),
    "list_directory": frozenset({Capability.READ_WORKSPACE}),
    "directory_tree": frozenset({Capability.READ_WORKSPACE}),
    "read": frozenset({Capability.READ_WORKSPACE}),
    "read_file": frozenset({Capability.READ_WORKSPACE}),
    "read_text_file": frozenset({Capability.READ_WORKSPACE}),
    "read_multiple_files": frozenset({Capability.READ_WORKSPACE}),
    "read_outline": frozenset({Capability.READ_WORKSPACE}),
    "search_codebase": frozenset({Capability.READ_WORKSPACE}),
    "search_files": frozenset({Capability.READ_WORKSPACE}),
    "get_file_info": frozenset({Capability.READ_WORKSPACE}),
    "list_allowed_directories": frozenset({Capability.READ_WORKSPACE}),
    "edit": frozenset({Capability.READ_WORKSPACE, Capability.WRITE_WORKSPACE}),
    "edit_file": frozenset({Capability.READ_WORKSPACE, Capability.WRITE_WORKSPACE}),
    "write": frozenset({Capability.WRITE_WORKSPACE, Capability.CREATE_FILE}),
    "write_file": frozenset({Capability.WRITE_WORKSPACE, Capability.CREATE_FILE}),
    "create_directory": frozenset({
        Capability.WRITE_WORKSPACE,
        Capability.CREATE_FILE,
    }),
    "run": frozenset({Capability.EXECUTE_PROCESS}),
    "bash": frozenset({Capability.EXECUTE_PROCESS}),
    "apply_patch": frozenset({
        Capability.WRITE_WORKSPACE,
        Capability.APPLY_VERIFIED_PATCH,
    }),
    "delegate": frozenset({Capability.DELEGATE_ACTOR}),
    "update_state": frozenset(),
}

_CODER_CAPABILITIES = frozenset({
    Capability.READ_WORKSPACE,
    Capability.WRITE_WORKSPACE,
    Capability.CREATE_FILE,
    Capability.EXECUTE_PROCESS,
    Capability.GIT_READ,
    # These are maximum capabilities, not unconditional grants. The
    # deterministic command middleware still requires an action-bound approval.
    Capability.NETWORK_ACCESS,
    Capability.CHANGE_DEPENDENCIES,
    Capability.EXTERNAL_SIDE_EFFECT,
})

ROLE_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "scout": frozenset({Capability.READ_WORKSPACE, Capability.GIT_READ}),
    "coder": _CODER_CAPABILITIES,
    "verifier": frozenset({
        Capability.READ_WORKSPACE,
        Capability.CREATE_FILE,
        Capability.EXECUTE_PROCESS,
        Capability.GIT_READ,
    }),
    "planner": frozenset({
        Capability.READ_WORKSPACE,
        Capability.WRITE_WORKSPACE,
        Capability.APPLY_VERIFIED_PATCH,
        Capability.DELEGATE_ACTOR,
    }),
    # Explicit bounded compatibility roles for older call sites.
    "actor": _CODER_CAPABILITIES,
    "legacy": _CODER_CAPABILITIES,
}


__all__ = ["ROLE_CAPABILITIES", "TOOL_CAPABILITIES"]
