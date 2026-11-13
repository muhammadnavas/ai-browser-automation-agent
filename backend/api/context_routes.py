"""
backend/api/context_routes.py
-----------------------------
API routes for My Context:
- Profile CRUD (Phase 1)
- Document Vault (Phase 2)
- Persistent Memory (Phase 3)
- Context Retrieval (Phase 4)

Enforces authenticated session ownership, CSRF protection on mutations,
parameterized queries, and strict input validation.
"""

import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status, UploadFile, File, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db_session, is_db_available
from backend.security.auth import require_session, get_authenticated_user_id, SessionData
from backend.services.profile_service import profile_service
from backend.services.document_service import document_service, MAX_FILE_SIZE_BYTES
from backend.services.memory_service import memory_service
from backend.services.context_retrieval_service import context_retrieval_service

router = APIRouter(prefix="/context", tags=["context"])
logger = logging.getLogger("backend.api.context")


# ===========================================================================
# Schemas
# ===========================================================================

class ProfileUpdateRequest(BaseModel):
    profile_data: Dict[str, Any] = Field(
        ...,
        description="Structured dictionary containing personal details, education, skills, and preferences.",
    )


class ProfileResponse(BaseModel):
    id: Optional[str] = None
    user_id: str
    profile_data: Dict[str, Any]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    exists: bool = False


class DocumentResponse(BaseModel):
    id: str
    user_id: str
    filename: str
    file_extension: Optional[str] = None
    content_type: str
    file_size: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    extracted_text: Optional[str] = None
    has_text: bool = False
    extraction_status: str = "completed"


class MemoryCreateRequest(BaseModel):
    memory_content: str = Field(..., min_length=1, max_length=2000)
    category: Optional[str] = Field("Personal", max_length=64)


class MemoryUpdateRequest(BaseModel):
    memory_content: Optional[str] = Field(None, min_length=1, max_length=2000)
    category: Optional[str] = Field(None, max_length=64)


class MemoryResponse(BaseModel):
    id: str
    user_id: str
    memory_content: str
    category: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ContextRetrieveRequest(BaseModel):
    goal: str = Field(..., min_length=1)
    max_documents: Optional[int] = Field(2, ge=1, le=5)
    max_memories: Optional[int] = Field(5, ge=1, le=10)


# ===========================================================================
# Status / Health
# ===========================================================================

@router.get("/status")
async def get_context_status(
    request: Request,
    session: SessionData = Depends(require_session),
):
    """Check health and availability of My Context database persistence."""
    return {
        "status": "active" if is_db_available() else "unavailable",
        "db_available": is_db_available(),
        "user_id": get_authenticated_user_id(session),
    }


# ===========================================================================
# Phase 1: Profile Endpoints
# ===========================================================================

@router.get("/profile", response_model=ProfileResponse)
async def get_user_profile(
    request: Request,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """Retrieve personal profile for the authenticated session."""
    user_id = get_authenticated_user_id(session)
    result = await profile_service.get_profile_dict(db, user_id)
    return ProfileResponse(**result)


@router.post("/profile", response_model=ProfileResponse)
async def create_or_update_user_profile(
    payload: ProfileUpdateRequest,
    request: Request,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """Create or update personal profile for the authenticated session."""
    user_id = get_authenticated_user_id(session)
    await profile_service.upsert_profile(db, user_id, payload.profile_data)
    result = await profile_service.get_profile_dict(db, user_id)
    return ProfileResponse(**result)


@router.put("/profile", response_model=ProfileResponse)
async def update_user_profile(
    payload: ProfileUpdateRequest,
    request: Request,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """Update personal profile for the authenticated session."""
    user_id = get_authenticated_user_id(session)
    await profile_service.upsert_profile(db, user_id, payload.profile_data)
    result = await profile_service.get_profile_dict(db, user_id)
    return ProfileResponse(**result)


@router.delete("/profile")
async def delete_user_profile(
    request: Request,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """Delete the personal profile for the authenticated session."""
    user_id = get_authenticated_user_id(session)
    deleted = await profile_service.delete_profile(db, user_id)
    return {
        "status": "deleted" if deleted else "not_found",
        "user_id": user_id,
        "success": True,
    }


# ===========================================================================
# Phase 2: Document Vault Endpoints
# ===========================================================================

def _doc_to_response(doc, include_text: bool = False) -> DocumentResponse:
    has_text = bool(doc.extracted_text and doc.extracted_text.strip() and not doc.extracted_text.startswith("[Scanned"))
    status_str = "completed" if has_text else ("scanned/empty" if doc.extracted_text else "pending")
    ext = getattr(doc, "file_extension", None) or os.path.splitext(doc.filename)[1].lower() if doc.filename else None
    return DocumentResponse(
        id=str(doc.id),
        user_id=doc.user_id,
        filename=doc.filename,
        file_extension=ext,
        content_type=doc.content_type,
        file_size=doc.file_size,
        created_at=doc.created_at.isoformat() if doc.created_at else None,
        updated_at=doc.updated_at.isoformat() if doc.updated_at else None,
        extracted_text=doc.extracted_text if include_text else None,
        has_text=has_text,
        extraction_status=status_str,
    )


@router.post("/documents", response_model=DocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    request: Request = None,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """Securely upload and parse a personal document (PDF, DOCX, TXT)."""
    user_id = get_authenticated_user_id(session)

    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename in upload.")

    content = await file.read(MAX_FILE_SIZE_BYTES + 1024)
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File size exceeds maximum allowed limit of 10 MB.")

    try:
        doc = await document_service.upload_document(
            db=db,
            user_id=user_id,
            filename=file.filename,
            content_type=file.content_type or "application/octet-stream",
            content=content,
        )
        return _doc_to_response(doc, include_text=False)
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as exc:
        logger.error("Document upload failed: %s", exc)
        raise HTTPException(status_code=500, detail="Document could not be processed and saved.")


@router.get("/documents", response_model=List[DocumentResponse])
async def list_documents(
    request: Request,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """List all personal documents for the authenticated user."""
    user_id = get_authenticated_user_id(session)
    docs = await document_service.list_documents(db, user_id)
    return [_doc_to_response(d, include_text=False) for d in docs]


@router.get("/documents/{document_id}", response_model=DocumentResponse)
async def get_document_details(
    document_id: str,
    request: Request,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """Retrieve document metadata and extracted text."""
    user_id = get_authenticated_user_id(session)
    doc = await document_service.get_document(db, user_id, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    return _doc_to_response(doc, include_text=True)


@router.get("/documents/{document_id}/text")
async def get_document_extracted_text(
    document_id: str,
    request: Request,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """Retrieve extracted plaintext for a document."""
    user_id = get_authenticated_user_id(session)
    doc = await document_service.get_document(db, user_id, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    return {
        "id": str(doc.id),
        "filename": doc.filename,
        "extracted_text": doc.extracted_text or "",
    }


@router.get("/documents/{document_id}/download")
async def download_document(
    document_id: str,
    request: Request,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """Securely download the raw document file, enforcing ownership isolation."""
    from urllib.parse import quote
    user_id = get_authenticated_user_id(session)
    file_result = await document_service.get_document_file(db, user_id, document_id)
    if not file_result:
        raise HTTPException(status_code=404, detail="Document file not found.")
    stream, filename, content_type = file_result
    safe_filename = quote(filename)
    return StreamingResponse(
        stream,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"; filename*=UTF-8\'\'{safe_filename}',
        },
    )


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: str,
    request: Request,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """Delete document metadata and remove the file from private storage."""
    user_id = get_authenticated_user_id(session)
    deleted = await document_service.delete_document(db, user_id, document_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found.")
    return {"status": "deleted", "id": document_id, "success": True}


# ===========================================================================
# Phase 3: Persistent Memory Endpoints
# ===========================================================================

def _mem_to_response(mem) -> MemoryResponse:
    return MemoryResponse(
        id=str(mem.id),
        user_id=mem.user_id,
        memory_content=mem.memory_content,
        category=mem.category,
        created_at=mem.created_at.isoformat() if mem.created_at else None,
        updated_at=mem.updated_at.isoformat() if mem.updated_at else None,
    )


@router.post("/memories", response_model=MemoryResponse)
async def create_memory(
    payload: MemoryCreateRequest,
    request: Request,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """Create a persistent user memory entry."""
    user_id = get_authenticated_user_id(session)
    try:
        mem = await memory_service.create_memory(
            db=db,
            user_id=user_id,
            memory_content=payload.memory_content,
            category=payload.category,
        )
        return _mem_to_response(mem)
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))


@router.get("/memories", response_model=List[MemoryResponse])
async def list_memories(
    category: Optional[str] = Query(None, description="Optional category filter"),
    request: Request = None,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """List persistent memories belonging to the authenticated user."""
    user_id = get_authenticated_user_id(session)
    mems = await memory_service.list_memories(db, user_id, category=category)
    return [_mem_to_response(m) for m in mems]


@router.get("/memories/{memory_id}", response_model=MemoryResponse)
async def get_memory(
    memory_id: str,
    request: Request,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """Retrieve a single persistent memory entry."""
    user_id = get_authenticated_user_id(session)
    mem = await memory_service.get_memory(db, user_id, memory_id)
    if not mem:
        raise HTTPException(status_code=404, detail="Memory entry not found.")
    return _mem_to_response(mem)


@router.put("/memories/{memory_id}", response_model=MemoryResponse)
async def update_memory(
    memory_id: str,
    payload: MemoryUpdateRequest,
    request: Request,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """Update an existing persistent memory entry."""
    user_id = get_authenticated_user_id(session)
    try:
        mem = await memory_service.update_memory(
            db=db,
            user_id=user_id,
            memory_id=memory_id,
            memory_content=payload.memory_content,
            category=payload.category,
        )
        if not mem:
            raise HTTPException(status_code=404, detail="Memory entry not found.")
        return _mem_to_response(mem)
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))


@router.delete("/memories/{memory_id}")
async def delete_memory(
    memory_id: str,
    request: Request,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """Delete a persistent memory entry."""
    user_id = get_authenticated_user_id(session)
    deleted = await memory_service.delete_memory(db, user_id, memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory entry not found.")
    return {"status": "deleted", "id": memory_id, "success": True}


# ===========================================================================
# Phase 4: Context Retrieval Endpoint
# ===========================================================================

@router.post("/retrieve")
async def retrieve_context(
    payload: ContextRetrieveRequest,
    request: Request,
    session: SessionData = Depends(require_session),
    db: AsyncSession = Depends(get_db_session),
):
    """Retrieve relevant context (profile, documents, memories) for a task goal."""
    user_id = get_authenticated_user_id(session)
    result = await context_retrieval_service.retrieve_relevant_context(
        db=db,
        user_id=user_id,
        goal=payload.goal,
        max_documents=payload.max_documents or 2,
        max_memories=payload.max_memories or 5,
    )
    return result
