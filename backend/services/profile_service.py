"""
backend/services/profile_service.py
-----------------------------------
Service layer for managing persistent user profiles in PostgreSQL.
Enforces authenticated ownership, parameterized queries, and profile structure.
"""

import logging
from typing import Any, Dict, Optional
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from backend.db.models import UserProfile

logger = logging.getLogger("backend.services.profile")

ALLOWED_PROFILE_SECTIONS = {
    "personal_details",
    "education",
    "skills_and_experience",
    "career_interests",
    "job_preferences",
    "custom_instructions",
    "full_name",
    "email",
    "phone",
    "location",
    "summary",
    "skills",
    "work_experience",
}


class ProfileService:
    """Manages UserProfile entities in PostgreSQL."""

    async def get_profile(self, db: AsyncSession, user_id: str) -> Optional[UserProfile]:
        """Fetch the profile belonging to the authenticated user."""
        stmt = select(UserProfile).where(UserProfile.user_id == user_id)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_profile_dict(self, db: AsyncSession, user_id: str) -> Dict[str, Any]:
        """Retrieve profile as a dictionary, returning default structure if not yet created."""
        profile = await self.get_profile(db, user_id)
        if not profile:
            return {
                "user_id": user_id,
                "profile_data": {
                    "personal_details": {},
                    "education": {},
                    "skills_and_experience": {},
                    "career_interests": "",
                    "job_preferences": "",
                    "custom_instructions": "",
                },
                "exists": False,
            }
        return {
            "id": str(profile.id),
            "user_id": profile.user_id,
            "profile_data": profile.profile_data or {},
            "created_at": profile.created_at.isoformat() if profile.created_at else None,
            "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
            "exists": True,
        }

    async def upsert_profile(
        self,
        db: AsyncSession,
        user_id: str,
        profile_data: Dict[str, Any],
    ) -> UserProfile:
        """Create or update the user's profile with safe parameterized binding."""
        if not isinstance(profile_data, dict):
            raise ValueError("profile_data must be a dictionary.")

        profile = await self.get_profile(db, user_id)
        if profile:
            # Merge updates into existing profile data
            merged_data = dict(profile.profile_data or {})
            merged_data.update(profile_data)
            profile.profile_data = merged_data
        else:
            profile = UserProfile(
                user_id=user_id,
                profile_data=profile_data,
            )
            db.add(profile)

        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            # Handle race condition in concurrent creation
            profile = await self.get_profile(db, user_id)
            if profile:
                merged_data = dict(profile.profile_data or {})
                merged_data.update(profile_data)
                profile.profile_data = merged_data
                await db.flush()
            else:
                raise

        return profile

    async def delete_profile(self, db: AsyncSession, user_id: str) -> bool:
        """Delete user profile. Returns True if deleted, False if no profile existed."""
        stmt = delete(UserProfile).where(UserProfile.user_id == user_id)
        result = await db.execute(stmt)
        return result.rowcount > 0


profile_service = ProfileService()

