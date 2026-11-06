import base64
from pathlib import Path
from nacl.signing import SigningKey, VerifyKey


class Signer:
    """Ed25519 signer. Generates the key on first use if it does not exist."""

    def __init__(self, key_path: str):
        p = Path(key_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            self._sk = SigningKey(p.read_bytes())
        else:
            self._sk = SigningKey.generate()
            p.write_bytes(self._sk.encode())
            p.chmod(0o600)

    def sign(self, payload: bytes) -> str:
        return base64.b64encode(self._sk.sign(payload).signature).decode()

    def fingerprint(self) -> str:
        return fingerprint(self.public_key_b64())

    def public_key_b64(self) -> str:
        return base64.b64encode(self._sk.verify_key.encode()).decode()

    @staticmethod
    def verify(public_key_b64: str, payload: bytes, signature_b64: str) -> bool:
        try:
            vk = VerifyKey(base64.b64decode(public_key_b64))
            vk.verify(payload, base64.b64decode(signature_b64))
            return True
        except Exception:
            return False

def fingerprint(public_key_b64: str) -> str:
    """Short, human-comparable SHA-256 fingerprint of an Ed25519 public key.
    Publish it out-of-band (e.g. in the hackathon submission) so a verifier
    does not have to trust the server that serves the key."""
    import hashlib
    h = hashlib.sha256(base64.b64decode(public_key_b64)).hexdigest()[:32]
    return ":".join(h[i:i + 4] for i in range(0, 32, 4))
