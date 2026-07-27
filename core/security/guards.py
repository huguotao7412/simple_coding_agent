"""Compatibility facade for content guard providers.

Provider implementations live in focused leaf modules so the optional OpenAI
dependency boundary remains obvious. Existing imports from ``security.guards``
continue to work.
"""

from .composite_guard import CompositeContentGuardProvider
from .content_guard import ContentGuardProvider
from .local_guard import LocalContentGuardProvider
from .openai_guard import OpenAIGuardrailsProvider

__all__ = [
    "CompositeContentGuardProvider",
    "ContentGuardProvider",
    "LocalContentGuardProvider",
    "OpenAIGuardrailsProvider",
]
