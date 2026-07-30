# ToolGateway

All model-generated local, MCP, run, patch, delegate and verification calls
enter the same PEP. Batch and resumed pending calls are authorized individually.

Execution order:

1. canonicalize the name and resolve registration;
2. inject trusted `workspace_dir` before authorization;
3. validate the registered schema;
4. derive capabilities and role bounds;
5. enforce workspace, destructive command, network, dependency and Git rules;
6. inspect an egress-safe TOOL_INTENT metadata summary;
7. validate/consume an exact single-use approval;
8. emit `tool_execution_started`;
9. dispatch a mutable copy of the immutable `AuthorizedToolCall`;
10. redact and byte-limit output;
11. mark reviewed output untrusted and run local TOOL_OUTPUT inspection;
12. emit `tool_execution_finished` and return `ToolResult`.

Unknown tools/capabilities and malformed schemas deny. Authorization exceptions
fail closed. Provider ALLOW cannot override deterministic DENY.

The approval fingerprint is SHA-256 over Run, Actor, role, normalized workspace,
canonical tool name, canonical final JSON arguments, capabilities, risk and
policy version. Grants expire, are exact-action bound, and are consumed once.

Role capability matrix:

| Role | Read | Write/create | Run/verify | Network/deps | Git mutation/secrets |
|---|---:|---:|---:|---:|---:|
| Scout | yes | no | no | no | no |
| Coder | yes | scoped | controlled | exact approval | no by default |
| Verifier | yes | no | verification only | no | no |
| Planner | planning | verified patch only | no arbitrary shell | no | no |

