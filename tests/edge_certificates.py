"""Certificates for the edge's ``files`` mode (ADR-0034): a private CA and a site certificate.

The unit tests of ``ops/checks.py edge`` read them, and the tests that run Caddy load them: the
same shapes an operator brings (a private CA's or a Cloudflare Origin CA certificate, its key, the
CA in the tunnel's pool).
"""

import datetime
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass(frozen=True)
class Validity:
    """Days from now: negative values lie in the past."""

    starts: int = -1
    ends: int = 90


A_QUARTER = Validity()


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _san(names: list[str]) -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = []
    for name in names:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    return x509.SubjectAlternativeName(entries)


def _pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def write_edge_files(
    directory: Path,
    names: list[str],
    validity: Validity = A_QUARTER,
    *,
    ca_name: str = "gmat-m8 test CA",
) -> None:
    """``tls/cert.pem`` and ``tls/key.pem`` naming ``names``, issued by a CA written to
    ``tunnel/ca.pem``, as the runbook lays them out. The key is mode 0400."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(_name(ca_name))
        .issuer_name(_name(ca_name))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=2))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(names[0]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + datetime.timedelta(days=validity.starts))
        .not_valid_after(now + datetime.timedelta(days=validity.ends))
        .add_extension(_san(names), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    (directory / "tls").mkdir(parents=True, exist_ok=True)
    (directory / "tunnel").mkdir(parents=True, exist_ok=True)
    (directory / "tls" / "cert.pem").write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
    )
    key_path = directory / "tls" / "key.pem"
    key_path.unlink(missing_ok=True)
    key_path.write_bytes(_pem_key(key))
    key_path.chmod(0o400)
    (directory / "tunnel" / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
