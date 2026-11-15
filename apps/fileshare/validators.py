import mimetypes
import os
from django.conf import settings
from rest_framework.exceptions import ValidationError

ALLOWED_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
    ".txt", ".md", ".csv", ".json", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx", ".zip",
}

ALLOWED_MIME_PREFIXES = (
    "image/",
    "text/",
    "application/pdf",
    "application/json",
    "application/zip",
    "application/x-zip-compressed",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.",
    "application/vnd.ms-",
    "application/octet-stream",
)


def validate_uploaded_file(uploaded_file):
    if not uploaded_file:
        raise ValidationError("No file provided.")

    if uploaded_file.size <= 0:
        raise ValidationError("Empty files cannot be uploaded.")

    if uploaded_file.size > settings.MAX_UPLOAD_SIZE_BYTES:
        raise ValidationError(f"File exceeds maximum size of {settings.MAX_UPLOAD_SIZE_MB} MB.")

    safe_name = os.path.basename(uploaded_file.name)
    _, ext = os.path.splitext(safe_name.lower())
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(f"File type '{ext or 'unknown'}' is not permitted.")

    content_type = (getattr(uploaded_file, "content_type", "") or "").lower()
    guessed_type, _ = mimetypes.guess_type(safe_name)
    effective_mime = content_type or guessed_type or "application/octet-stream"

    if not any(effective_mime.startswith(prefix) for prefix in ALLOWED_MIME_PREFIXES):
        raise ValidationError(f"MIME type '{effective_mime}' is not allowed.")

    return safe_name, effective_mime
