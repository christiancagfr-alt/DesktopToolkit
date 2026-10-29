from __future__ import annotations

import json
import hashlib
import secrets
import socket
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

import gdrive_client
import notebook_ui
from lan_share import LanShareClient, LanShareServer
from p2p_transfer import (
    P2PSession,
    _chunk_aad,
    _derive_transfer_key,
    _done_proof,
    _offer_aad,
)
from secret_store import _looks_secret_key
from transport_security import make_request_auth, verify_request_auth
from zip_safe import safe_extractall


class SecurityRegressionTests(unittest.TestCase):
    def test_zip_slip_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escape.txt", "bad")
            with zipfile.ZipFile(archive) as zf:
                with self.assertRaisesRegex(ValueError, "危险压缩路径"):
                    safe_extractall(zf, root / "out")
            self.assertFalse((root / "escape.txt").exists())

    def test_zip_expansion_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "large.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("large.bin", b"x" * 20)
            with zipfile.ZipFile(archive) as zf:
                with self.assertRaisesRegex(ValueError, "总大小"):
                    safe_extractall(zf, root / "out", max_total_size=10)

    def test_weak_lan_passwords_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self.assertIn("至少 8 位", LanShareServer().start(raw, "short"))

    def test_lan_share_is_https_and_password_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "hello.txt").write_text("encrypted hello", encoding="utf-8")
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            server = LanShareServer()
            try:
                self.assertIn("HTTPS 加密", server.start(root, "correct-password", port))
                client = LanShareClient()
                self.assertIn("已连接", client.connect("127.0.0.1", port, "correct-password"))
                self.assertTrue(client.base_url.startswith("https://"))
                error, entries = client.list_dir()
                self.assertIsNone(error)
                self.assertIn("hello.txt", {entry["name"] for entry in entries})
                downloaded = root / "downloaded.txt"
                self.assertIn("已下载", client.download("hello.txt", downloaded))
                self.assertEqual(downloaded.read_text(encoding="utf-8"), "encrypted hello")
                upload_source = root / "upload-source.txt"
                upload_source.write_text("encrypted upload", encoding="utf-8")
                self.assertIn("已上传", client.upload(upload_source, "uploads"))
                self.assertEqual(
                    (root / "uploads" / upload_source.name).read_text(encoding="utf-8"),
                    "encrypted upload",
                )
                wrong = LanShareClient().connect("127.0.0.1", port, "wrong-password")
                self.assertIn("密码错误", wrong)
            finally:
                server.stop()

    def test_lan_auth_is_bound_to_certificate_and_replay_safe(self) -> None:
        key = secrets.token_bytes(32)
        fingerprint = "ab" * 32
        header = make_request_auth(key, fingerprint, "GET", "/api/ping")
        seen: dict[str, float] = {}
        self.assertTrue(
            verify_request_auth(header, key, fingerprint, "GET", "/api/ping", seen)
        )
        self.assertFalse(
            verify_request_auth(header, key, fingerprint, "GET", "/api/ping", seen)
        )
        other_seen: dict[str, float] = {}
        self.assertFalse(
            verify_request_auth(header, key, "cd" * 32, "GET", "/api/ping", other_seen)
        )

    def test_p2p_chunks_use_authenticated_encryption(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt = secrets.token_bytes(16)
        key = _derive_transfer_key("a-strong-shared-transfer-key", salt)
        transfer_id = secrets.token_hex(16)
        offer_nonce = secrets.token_bytes(12)
        sealed_offer = AESGCM(key).encrypt(
            offer_nonce, b'{"name":"secret.txt","size":5}', _offer_aad(transfer_id)
        )
        self.assertEqual(
            AESGCM(key).decrypt(offer_nonce, sealed_offer, _offer_aad(transfer_id)),
            b'{"name":"secret.txt","size":5}',
        )
        nonce = secrets.token_bytes(12)
        aad = _chunk_aad(transfer_id, 0, 1)
        encrypted = AESGCM(key).encrypt(nonce, b"hello", aad)
        self.assertEqual(AESGCM(key).decrypt(nonce, encrypted, aad), b"hello")
        digest = hashlib.sha256(b"hello").hexdigest()
        self.assertEqual(
            _done_proof(key, transfer_id, digest, 5),
            _done_proof(key, transfer_id, digest, 5),
        )

    def test_p2p_encryption_key_is_protected_at_rest(self) -> None:
        self.assertTrue(_looks_secret_key("p2p_encryption_key"))

    def test_p2p_encrypted_transfer_end_to_end(self) -> None:
        from websockets.sync.server import serve

        peers = set()
        peers_lock = threading.Lock()

        def relay(ws) -> None:
            with peers_lock:
                peers.add(ws)
            try:
                for message in ws:
                    with peers_lock:
                        targets = [peer for peer in peers if peer is not ws]
                    for peer in targets:
                        peer.send(message)
            finally:
                with peers_lock:
                    peers.discard(ws)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        server = serve(relay, "127.0.0.1", port)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        secret = "shared-e2ee-test-secret-123"
        room = "TESTROOM"
        sender_done = threading.Event()
        receiver_done = threading.Event()
        receiver_messages: list[str] = []
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "private-name.txt"
            source.write_bytes(b"confidential payload" * 1024)
            dest = root / "received"

            receiver = P2PSession(
                f"ws://127.0.0.1:{port}",
                room,
                secret,
                on_status=lambda msg: (
                    receiver_messages.append(msg),
                    receiver_done.set() if msg.startswith("✅") else None,
                ),
            )
            sender = P2PSession(
                f"ws://127.0.0.1:{port}",
                room,
                secret,
                on_status=lambda msg: sender_done.set() if msg.startswith("✅") else None,
            )
            try:
                receiver.receive_file_async(dest)
                time.sleep(0.2)
                sender.send_file_async(source)
                self.assertTrue(sender_done.wait(10), "sender did not complete")
                self.assertTrue(receiver_done.wait(10), "receiver did not complete")
                received = dest / source.name
                self.assertEqual(received.read_bytes(), source.read_bytes())
            finally:
                sender.stop()
                receiver.stop()
                server.shutdown()
                server_thread.join(timeout=3)

    def test_private_link_title_target_is_rejected(self) -> None:
        self.assertFalse(notebook_ui._is_public_http_url("http://127.0.0.1/admin"))
        self.assertFalse(notebook_ui._is_public_http_url("http://169.254.169.254/latest"))

    def test_google_oauth_endpoints_cannot_be_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "client.json"
            path.write_text(
                json.dumps(
                    {
                        "installed": {
                            "client_id": "example.apps.googleusercontent.com",
                            "client_secret": "not-a-real-secret",
                            "auth_uri": "https://attacker.invalid/auth",
                            "token_uri": "https://attacker.invalid/token",
                        }
                    }
                ),
                encoding="utf-8",
            )
            loaded = gdrive_client.load_client_secrets(path)
        self.assertEqual(loaded["auth_uri"], gdrive_client.AUTH_URL)
        self.assertEqual(loaded["token_uri"], gdrive_client.TOKEN_URL)


if __name__ == "__main__":
    unittest.main()
