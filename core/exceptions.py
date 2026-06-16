class SCAAgentError(Exception):
    """Base exception for all SCA errors."""
    pass


class ToolExecutionError(SCAAgentError):
    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        super().__init__(f"[{tool_name}] {message}")


class ToolSecurityError(SCAAgentError):
    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        super().__init__(f"[{tool_name}] SECURITY: {message}")


class ContextLimitError(SCAAgentError):
    pass


class LLMAPIError(SCAAgentError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"API error {status_code}: {message}")
