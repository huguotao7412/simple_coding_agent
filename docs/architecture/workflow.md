# Application Workflow

Only `AgentWorkflow.transition()` or `AgentWorkflow.resume()` changes a stage.
Thin LangGraph nodes invoke application use cases and store a cursor/interrupt.

| Stage | Input | Output | Owner |
|---|---|---|---|
| ingress security | raw request | redacted/denied request | content pipeline |
| task assessment | safe request | risk/capability assessment | application assessment |
| execution policy | assessment | immutable policy/budget | application policy use case |
| input approval | exact policy scope | grant/deny/pause | approval service |
| planning | safe request + policy | task plan | Planner |
| plan validation | plan + policy | validated plan | application validation |
| actor scheduling | task graph | ready IDs | delegation service |
| actor execution | task spec | handoff/artifact refs | Actor executor port |
| verification | artifact refs + gates | summary/log refs | verification service |
| bounded repair | failed summary + budget | new verification refs | repair coordinator |
| finalization | verified refs | answer draft | finalization service |
| final output security | draft | sanitized output/ref | content pipeline |
| persistence/report | aggregate + cursor | checkpoint/report | RunStore port |

Failures transition to `FAILED`; approval interrupts transition to `PAUSED`.
Terminal cursors reject transition and resume. Completed tool-call cache entries
reuse only sanitized observations; pending calls pass through `ToolGateway`
again.

