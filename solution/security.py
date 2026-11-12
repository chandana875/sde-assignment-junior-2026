import base64
import hashlib
import hmac
import os
from cryptography.fernet import Fernet

# Deterministic secret salt/key for reproducible runs across CLI invocations.
# Can be overridden via environment variables in production.
_SECRET_SALT = os.environ.get("PIPELINE_PII_SALT", "agentic-universe-sde-salt-2026").encode("utf-8")
_RAW_KEY = hashlib.sha256(_SECRET_SALT).digest()
_FERNET = Fernet(base64.urlsafe_b64encode(_RAW_KEY))


def hash_phone(canonical_phone: str) -> str:
    """Deterministic HMAC-SHA256 hash so events and leads link without plaintext PII."""
    return hmac.new(_SECRET_SALT, canonical_phone.encode("utf-8"), hashlib.sha256).hexdigest()


def mask_phone(canonical_phone: str) -> str:
    """Formats +91XXXXXXXXXX as +91******XXXX."""
    return f"+91******{canonical_phone[-4:]}"


def encrypt_pii(plaintext: str) -> str:
    """Encrypts non-lookup PII fields (e.g., customer_name) at rest."""
    return _FERNET.encrypt(plaintext.encode("utf-8")).decode("utf-8")