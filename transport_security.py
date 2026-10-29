"""Authenticated transport helpers for local HTTPS and encrypted transfers.

LAN services use an ephemeral self-signed TLS certificate. The certificate is
authenticated with a password-derived HMAC that is bound to its SHA-256
fingerprint, so an active proxy cannot relay credentials through a different
certificate. P2P file encryption uses a separate user-shared secret.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import shutil
import socket
import ssl
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


PBKDF2_ITERATIONS = 300_000
AUTH_CLOCK_SKEW_SECONDS = 180


@dataclass
class EphemeralTlsIdentity:
    context: ssl.SSLContext
    fingerprint: str
    temp_dir: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)


def create_ephemeral_tls_identity(common_name: str) -> EphemeralTlsIdentity:
    """Create an in-memory-lifetime EC certificate and ready server context."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, common_name or "Desktop Toolkit")]
    )
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    fingerprint = hashlib.sha256(cert_der).hexdigest()
    temp_dir = Path(tempfile.mkdtemp(prefix="desktop_toolkit_tls_"))
    cert_path = temp_dir / "cert.pem"
    key_path = temp_dir / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    try:
        cert_path.chmod(0o600)
        key_path.chmod(0o600)
    except OSError:
        pass
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert_path), str(key_path))
    return EphemeralTlsIdentity(context, fingerprint, temp_dir)


def authenticated_client_context() -> ssl.SSLContext:
    """TLS context whose ephemeral certificate is authenticated by our HMAC.

    CA verification is intentionally disabled because the LAN host creates a
    fresh self-signed certificate. Callers MUST bind authentication to the
    observed certificate fingerprint with :func:`derive_channel_key`.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    # The self-signed certificate is authenticated by a password-derived HMAC
    # bound to its observed SHA-256 fingerprint (see make/verify_request_auth).
    context.verify_mode = ssl.CERT_NONE  # nosec B501
    return context


def fetch_tls_fingerprint(host: str, port: int, timeout: float = 5.0) -> str:
    context = authenticated_client_context()
    with socket.create_connection((host, int(port)), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise RuntimeError("服务器未提供 TLS 证书")
    return hashlib.sha256(der).hexdigest()


def derive_channel_key(secret: str, fingerprint: str, purpose: str) -> bytes:
    try:
        fp = bytes.fromhex(fingerprint)
    except ValueError as exc:
        raise ValueError("证书指纹无效") from exc
    salt = hashlib.sha256(purpose.encode("utf-8") + b"\0" + fp).digest()
    return hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), salt, PBKDF2_ITERATIONS, dklen=32
    )


def make_request_auth(key: bytes, fingerprint: str, method: str, target: str) -> str:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    payload = "\n".join(
        ("v1", timestamp, nonce, method.upper(), target, fingerprint.lower())
    ).encode("utf-8")
    proof = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return f"v1:{timestamp}:{nonce}:{proof}"


def verify_request_auth(
    header: str,
    key: bytes,
    fingerprint: str,
    method: str,
    target: str,
    seen_nonces: dict[str, float],
) -> bool:
    try:
        version, timestamp, nonce, proof = (header or "").split(":", 3)
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now = time.time()
    if version != "v1" or abs(now - ts) > AUTH_CLOCK_SKEW_SECONDS:
        return False
    if len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce.lower()):
        return False
    for old_nonce, used_at in list(seen_nonces.items()):
        if now - used_at > AUTH_CLOCK_SKEW_SECONDS:
            seen_nonces.pop(old_nonce, None)
    if nonce in seen_nonces:
        return False
    payload = "\n".join(
        ("v1", timestamp, nonce, method.upper(), target, fingerprint.lower())
    ).encode("utf-8")
    expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
    if not secrets.compare_digest(proof.lower(), expected):
        return False
    seen_nonces[nonce] = now
    return True
