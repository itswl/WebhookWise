"""Generate throwaway TLS material for the fake Feishu app-API host.

The custom-app transport posts to the fixed host ``https://open.feishu.cn``
through an httpx client built with ``trust_env=False``, so the only trust
root it consults is certifi's bundled ``cacert.pem``. This script runs once
inside the application image (which pins ``cryptography``) and fills the
shared ``e2e-certs`` volume with:

- ``server.pem`` / ``server.key``: a cert for ``open.feishu.cn`` signed by a
  fresh throwaway CA, used by ``fake_feishu.py``'s port-443 listener.
- ``ca.pem``: the CA certificate on its own, for debugging.
- a shadow of the image's ``certifi`` package (``__init__.py`` etc.) whose
  ``cacert.pem`` is the original bundle plus the throwaway CA. The app
  containers bind the volume over ``site-packages/certifi`` so outbound TLS
  trusts the fake server without any runtime-code or env changes.

Everything is regenerated per compose run (`down -v` drops the volume) and
never leaves the e2e network, so the private key needs no protection.
"""

from __future__ import annotations

import datetime
import shutil
import sys
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HOSTNAME = "open.feishu.cn"
VALIDITY = datetime.timedelta(days=7)


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _build_certificates() -> tuple[bytes, bytes, bytes]:
    """Return (ca_cert_pem, server_cert_pem, server_key_pem)."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = _new_key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "WebhookWise E2E CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    server_key = _new_key()
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOSTNAME)]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + VALIDITY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOSTNAME)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    server_key_pem = server_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return (
        ca_cert.public_bytes(serialization.Encoding.PEM),
        server_cert.public_bytes(serialization.Encoding.PEM),
        server_key_pem,
    )


def _shadow_certifi_package(out_dir: Path, ca_pem: bytes) -> None:
    """Copy the certifi package files and append the e2e CA to its bundle."""
    certifi_dir = Path(certifi.__file__).resolve().parent
    for source in sorted(certifi_dir.glob("*.py")):
        shutil.copyfile(source, out_dir / source.name)
    py_typed = certifi_dir / "py.typed"
    if py_typed.is_file():
        shutil.copyfile(py_typed, out_dir / py_typed.name)
    original_bundle = Path(certifi.where()).read_bytes()
    (out_dir / "cacert.pem").write_bytes(original_bundle + b"\n" + ca_pem)


def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/e2e-certs")
    out_dir.mkdir(parents=True, exist_ok=True)

    ca_pem, server_cert_pem, server_key_pem = _build_certificates()
    (out_dir / "ca.pem").write_bytes(ca_pem)
    (out_dir / "server.pem").write_bytes(server_cert_pem)
    (out_dir / "server.key").write_bytes(server_key_pem)
    _shadow_certifi_package(out_dir, ca_pem)

    # cert-init runs as root to own the fresh volume; the app user only reads.
    for path in out_dir.iterdir():
        path.chmod(0o644)
    print(f"e2e TLS material for {HOSTNAME} written to {out_dir}")


if __name__ == "__main__":
    main()
