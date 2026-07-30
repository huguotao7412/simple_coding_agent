from ..tool_security import SecurityMiddleware


class ToolAuthorizationPolicy(SecurityMiddleware):
    """Canonical name for deterministic final-argument authorization."""


__all__ = ["ToolAuthorizationPolicy"]
