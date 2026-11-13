"""
backend/services/memory_service.py
----------------------------------
Service layer for Persistent Memory (Phase 3):
- User-controlled memory creation, editing, deletion, and listing
- Multi-category support (Personal, Professional, Preferences, Instructions, Other)
- Security enforcement: blocks Tier 1 secrets (passwords, tokens, private keys)
- Parameterized database operations with strict user ownership isolation
"""

import uuid
import logging
from typing import Any, Dict, List, Optional
from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Memory
from backend.security import contains_tier1_secrets, contains_tier2_identifiers

logger = logging.getLogger("backend.services.memory")

ALLOWED_CATEGORIES = {
    "Personal",
    "Professional",
    "Preferences",
    "Instructions",
    "Other",
}

MAX_MEMORY_CHARS = 2000


def normalize_category(category: Optional[str]) -> str:
    """Normalize and validate memory category."""
    if not category or not category.strip():
        return "Personal"
    cat_clean = category.strip()
    for allowed in ALLOWED_CATEGORIES:
        if cat_clean.lower() == allowed.lower():
            return allowed
    return "Other"


def validate_memory_content(content: str) -> None:
    """Validate that memory content is non-empty and contains no sensitive credentials."""
    if not content or not content.strip():
        raise ValueError("Memory content cannot be empty.")

    if len(content) > MAX_MEMORY_CHARS:
        raise ValueError(f"Memory content exceeds maximum allowed length of {MAX_MEMORY_CHARS} characters.")

    # Security check: Never allow saving passwords, OTPs, private keys, or API secrets into memory
    if contains_tier1_secrets(content):
        raise ValueError(
            "Security policy violation: Memory content contains sensitive credentials, "
            "private keys, or authentication tokens (Tier 1). Credentials must not be stored in persistent memory."
        )

    if contains_tier2_identifiers(content):
        raise ValueError(
            "Security policy violation: Memory content contains unmasked personal identifiers "
            "such as credit card numbers or government IDs (Tier 2). Please remove sensitive identifiers before storing."
        )


class MemoryService:
    """Manages user-controlled persistent memories in PostgreSQL."""

    async def create_memory(
        self,
        db: AsyncSession,
        user_id: str,
        memory_content: str,
        category: Optional[str] = None,
    ) -> Memory:
        """Create a new validated memory entry for the user."""
        validate_memory_content(memory_content)
        norm_cat = normalize_category(category)

        mem = Memory(
            id=uuid.uuid4(),
            user_id=user_id,
            memory_content=memory_content.strip(),
            category=norm_cat,
        )
        db.add(mem)
        await db.flush()
        await db.refresh(mem)
        return mem

    async def list_memories(
        self,
        db: AsyncSession,
        user_id: str,
        category: Optional[str] = None,
    ) -> List[Memory]:
        """List all memories for the authenticated user, optionally filtered by category."""
        stmt = select(Memory).where(Memory.user_id == user_id)
        if category and category.strip():
            norm_cat = normalize_category(category)
            stmt = stmt.where(Memory.category == norm_cat)
        stmt = stmt.order_by(Memory.created_at.desc())

        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_memory(
        self,
        db: AsyncSession,
        user_id: str,
        memory_id: str,
    ) -> Optional[Memory]:
        """Retrieve a specific memory entry, verifying ownership."""
        try:
            mem_uuid = uuid.UUID(memory_id)
        except (ValueError, TypeError):
            return None

        stmt = select(Memory).where(
            Memory.id == mem_uuid,
            Memory.user_id == user_id,
        )
        result = await db.execute(stmt)
        return result.scalars().first()

    async def update_memory(
        self,
        db: AsyncSession,
        user_id: str,
        memory_id: str,
        memory_content: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Optional[Memory]:
        """Update an existing memory entry, validating content and enforcing ownership."""
        mem = await self.get_memory(db, user_id, memory_id)
        if not mem:
            return None

        if memory_content is not None:
            validate_memory_content(memory_content)
            mem.memory_content = memory_content.strip()

        if category is not None:
            mem.category = normalize_category(category)

        await db.flush()
        await db.refresh(mem)
        return mem

    async def delete_memory(
        self,
        db: AsyncSession,
        user_id: str,
        memory_id: str,
    ) -> bool:
        """Delete a memory entry belonging to the user."""
        try:
            mem_uuid = uuid.UUID(memory_id)
        except (ValueError, TypeError):
            return False

        stmt = delete(Memory).where(
            Memory.id == mem_uuid,
            Memory.user_id == user_id,
        )
        result = await db.execute(stmt)
        return result.rowcount > 0


memory_service = MemoryService()
