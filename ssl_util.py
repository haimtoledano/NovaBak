"""
ssl_util.py — Auto-generates a self-signed SSL certificate for HTTPS.

On first run, creates cert.pem + key.pem in the data/ directory.
On subsequent runs, checks if the cert is still valid (renews if < 30 days left).
No external tools required — uses the `cryptography` library.
"""
import os
import datetime
from config_env import DATA_DIR
from logger_util import log_info, log_warn

CERT_FILE = os.path.join(DATA_DIR, "cert.pem")
KEY_FILE  = os.path.join(DATA_DIR, "key.pem")
CERT_DAYS = 3650  # 10 years — internal use, no need to renew often


def _generate_cert():
    """Generate a self-signed RSA certificate and save to data/."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import ipaddress

    log_info("[SSL] Generating new self-signed certificate...")

    # Private key
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # Certificate subject
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "IL"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "THIS Cyber Security"),
        x509.NameAttribute(NameOID.COMMON_NAME, "NovaBak"),
    ])

    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=CERT_DAYS))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.DNSName("novabak"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    # Save key
    with open(KEY_FILE, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))

    # Save cert
    with open(CERT_FILE, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    log_info(f"[SSL] Certificate saved: {CERT_FILE}")
    log_info(f"[SSL] Valid for {CERT_DAYS} days ({now.date()} -> {(now + datetime.timedelta(days=CERT_DAYS)).date()})")


def _cert_needs_renewal(days_threshold=30):
    """Returns True if cert doesn't exist or expires within days_threshold days."""
    if not os.path.exists(CERT_FILE) or not os.path.exists(KEY_FILE):
        return True
    try:
        from cryptography import x509
        with open(CERT_FILE, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        remaining = cert.not_valid_after_utc.replace(tzinfo=None) - datetime.datetime.utcnow()
        if remaining.days < days_threshold:
            log_warn(f"[SSL] Certificate expires in {remaining.days} days — renewing.")
            return True
        log_info(f"[SSL] Certificate valid for {remaining.days} more days.")
        return False
    except Exception as e:
        log_warn(f"[SSL] Could not read certificate ({e}) — regenerating.")
        return True


def ensure_ssl_cert():
    """
    Ensures a valid SSL certificate exists in data/.
    Call this before starting uvicorn.
    Returns (cert_path, key_path).
    """
    if _cert_needs_renewal():
        _generate_cert()
    return CERT_FILE, KEY_FILE
