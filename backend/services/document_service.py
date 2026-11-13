"""
backend/services/document_service.py
------------------------------------
Service layer for Document Vault (Phase 2):
- Secure document validation (extensions, MIME types, magic bytes, size limits)
- Private server-side storage with UUID-based filenames
- Text extraction for PDF, DOCX, and TXT files
- Database metadata tracking in PostgreSQL
- Strict user ownership isolation and atomic cleanup on failures
"""

import io
import os
import re
import uuid
import zipfile
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select, delete
from sqlalchemy.orm import defer, undefer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Document

logger = logging.getLogger("backend.services.documents")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DOCUMENT_STORAGE_DIR = PROJECT_ROOT / "backend" / "data" / "documents"

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_EXTRACTED_TEXT_CHARS = 500_000      # 500k characters max extraction cap

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "application/octet-stream",  # Sometimes sent by browsers for docx/txt
}


def ensure_storage_dir(user_id: str) -> Path:
    """Ensure the user's private document storage directory exists."""
    safe_user = re.sub(r"[^a-zA-Z0-9_\-]", "_", user_id)
    user_dir = DOCUMENT_STORAGE_DIR / safe_user
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent directory traversal and remove illegal characters."""
    clean_name = os.path.basename(filename).strip()
    clean_name = re.sub(r'[\r\n\t\x00-\x1f\x7f-\x9f]', '', clean_name)
    clean_name = re.sub(r'[^a-zA-Z0-9_\-\.\(\) ]', '_', clean_name)
    if not clean_name or clean_name in ('.', '..'):
        clean_name = "uploaded_document"
    return clean_name[:255]


def validate_file_signature(content: bytes, ext: str) -> Tuple[bool, str]:
    """Validate file magic bytes / signature matching the declared extension."""
    if not content or len(content) == 0:
        return False, "Uploaded file is empty (0 bytes)."

    if len(content) > MAX_FILE_SIZE_BYTES:
        return False, f"File size exceeds maximum allowed limit of {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB."

    if ext == ".pdf":
        # PDF magic bytes: %PDF-
        if not content.startswith(b"%PDF-"):
            return False, "Invalid PDF file: Missing %PDF- header signature."
        return True, ""

    elif ext == ".docx":
        # DOCX is a zip file starting with PK\x03\x04
        if not content.startswith(b"PK\x03\x04"):
            return False, "Invalid DOCX file: Missing ZIP/DOCX header signature."
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                namelist = zf.namelist()
                # Must contain word/document.xml or [Content_Types].xml
                if "[Content_Types].xml" not in namelist and not any(n.startswith("word/") for n in namelist):
                    return False, "Invalid DOCX file: Missing Word document structure."
        except Exception as exc:
            return False, f"Invalid or corrupted DOCX archive: {exc}"
        return True, ""

    elif ext == ".txt":
        # Reject executable binaries or files containing null bytes
        if content.startswith(b"MZ") or content.startswith(b"\x7fELF"):
            return False, "Executable binaries are strictly prohibited."
        if b"\x00" in content[:4096]:
            return False, "Invalid text file: binary null bytes detected."
        return True, ""

    return False, f"Unsupported file extension: '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}."


def extract_text_from_bytes(content: bytes, ext: str) -> str:
    """Extract readable plaintext from supported file types."""
    extracted = ""
    try:
        if ext == ".pdf":
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            pages_text = []
            for i, page in enumerate(reader.pages):
                page_str = page.extract_text() or ""
                if page_str.strip():
                    pages_text.append(page_str.strip())
            if pages_text:
                extracted = "\n\n".join(pages_text)
            else:
                extracted = "[Scanned document: No selectable text detected. OCR is not enabled.]"

        elif ext == ".docx":
            import docx
            doc = docx.Document(io.BytesIO(content))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                    if row_text:
                        paragraphs.append(row_text)
            extracted = "\n".join(paragraphs)

        elif ext == ".txt":
            try:
                extracted = content.decode("utf-8")
            except UnicodeDecodeError:
                extracted = content.decode("latin-1", errors="replace")

    except Exception as exc:
        logger.warning("Error during text extraction for extension %s: %s", ext, exc)
        extracted = f"[Text extraction failed: {str(exc)}]"

    # Cap text length to prevent memory saturation
    return extracted[:MAX_EXTRACTED_TEXT_CHARS]


class DocumentService:
    """Manages secure document upload, private storage, extraction, and lifecycle."""

    async def upload_document(
        self,
        db: AsyncSession,
        user_id: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> Document:
        """Validate, store, extract text, and record a new document for the user."""
        clean_name = sanitize_filename(filename)
        ext = os.path.splitext(clean_name)[1].lower()

        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"Unsupported file format '{ext}'. Supported formats: PDF, DOCX, TXT.")

        is_valid, err_msg = validate_file_signature(content, ext)
        if not is_valid:
            raise ValueError(err_msg)

        doc_id = uuid.uuid4()
        user_dir = ensure_storage_dir(user_id)
        # Store file with UUID to prevent collisions and directory traversal
        storage_file_name = f"{doc_id.hex}_{ext.lstrip('.')}.bin"
        storage_path = user_dir / storage_file_name

        # Extract text before persisting
        extracted_text = extract_text_from_bytes(content, ext)

        # Attempt to write local disk cache (non-fatal; file_data is persisted directly in Neon)
        storage_path_str: Optional[str] = None
        try:
            with open(storage_path, "wb") as f:
                f.write(content)
            storage_path_str = str(storage_path)
        except Exception as exc:
            logger.info("Local disk cache write omitted or failed: %s (using PostgreSQL file_data storage)", exc)

        # Record in PostgreSQL with binary data so all collaborating developers can access it
        document = Document(
            id=doc_id,
            user_id=user_id,
            filename=clean_name,
            content_type=content_type or "application/octet-stream",
            file_size=len(content),
            storage_path=storage_path_str,
            extracted_text=extracted_text,
            file_data=content,
        )
        db.add(document)

        try:
            await db.flush()
            await db.refresh(document)
            document.__dict__["file_data"] = content
        except Exception as exc:
            # Atomic rollback: clean up orphaned file on disk if DB record fails
            if storage_path.exists():
                try:
                    storage_path.unlink()
                except Exception:
                    pass
            logger.error("Database insert failed for document %s: %s", doc_id, exc)
            raise

        return document

    async def list_documents(self, db: AsyncSession, user_id: str) -> List[Document]:
        """List all documents belonging to the authenticated user.
        
        Explicitly defers file_data so listing documents does not saturate memory with raw binaries.
        """
        stmt = (
            select(Document)
            .options(defer(Document.file_data))
            .where(Document.user_id == user_id)
            .order_by(Document.created_at.desc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_document(
        self,
        db: AsyncSession,
        user_id: str,
        document_id: str,
    ) -> Optional[Document]:
        """Retrieve a specific document, enforcing ownership isolation."""
        try:
            doc_uuid = uuid.UUID(document_id)
        except (ValueError, TypeError):
            return None

        stmt = select(Document).where(
            Document.id == doc_uuid,
            Document.user_id == user_id,
        )
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_document_text(
        self,
        db: AsyncSession,
        user_id: str,
        document_id: str,
    ) -> Optional[str]:
        """Retrieve extracted plaintext for a document, enforcing ownership isolation."""
        doc = await self.get_document(db, user_id, document_id)
        if not doc:
            return None
        return doc.extracted_text or ""

    async def get_document_file(
        self,
        db: AsyncSession,
        user_id: str,
        document_id: str,
    ) -> Optional[Tuple[io.BytesIO, str, str]]:
        """Retrieve raw file content as a stream, along with filename and content_type.
        
        Prefers direct binary data stored in Neon PostgreSQL (file_data), falling back
        to disk storage if file_data is not present. Explicitly undefer loading of file_data.
        """
        try:
            doc_uuid = uuid.UUID(document_id)
        except (ValueError, TypeError):
            return None

        stmt = select(Document).options(undefer(Document.file_data)).where(
            Document.id == doc_uuid,
            Document.user_id == user_id,
        )
        result = await db.execute(stmt)
        doc = result.scalars().first()
        if not doc:
            return None

        if doc.file_data is not None:
            return io.BytesIO(doc.file_data), doc.filename, doc.content_type

        if doc.storage_path and os.path.exists(doc.storage_path):
            with open(doc.storage_path, "rb") as f:
                data = f.read()
            return io.BytesIO(data), doc.filename, doc.content_type

        return None

    async def delete_document(
        self,
        db: AsyncSession,
        user_id: str,
        document_id: str,
    ) -> bool:
        """Delete document from PostgreSQL and remove its file from private storage."""
        doc = await self.get_document(db, user_id, document_id)
        if not doc:
            return False

        # Remove physical file
        if doc.storage_path:
            try:
                p = Path(doc.storage_path)
                if p.exists():
                    p.unlink()
            except Exception as exc:
                logger.warning("Could not delete stored file %s: %s", doc.storage_path, exc)

        # Remove database record
        stmt = delete(Document).where(
            Document.id == doc.id,
            Document.user_id == user_id,
        )
        await db.execute(stmt)
        return True


document_service = DocumentService()
