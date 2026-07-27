from __future__ import annotations

from typing import Protocol

from .models import ContentGuardAssessment, ContentGuardRequest


class ContentGuardProvider(Protocol):
    async def inspect(
        self,
        request: ContentGuardRequest,
    ) -> ContentGuardAssessment: ...


__all__ = ["ContentGuardProvider"]
