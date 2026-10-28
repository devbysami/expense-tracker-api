EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}

# The first bytes ("magic numbers") identify the real type, whatever the file is called.
SIGNATURES = {
    "jpg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "pdf": (b"%PDF-",),
}


def detect_type(head: bytes) -> str | None:
    for ext, prefixes in SIGNATURES.items():
        if head.startswith(prefixes):
            return ext
    return None


def key_prefix(user_id: int, expense_id: int) -> str:
    return f"receipts/{user_id}/{expense_id}/"
