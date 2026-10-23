# -*- coding: utf-8 -*-
"""
Logística Casa do Campo - Gerador e Gerenciador de Certificados SSL Locais
Permite acesso HTTPS para habilitar recursos que exigem contexto seguro (como GPS/Geolocalização no Chrome/Android/iOS).
"""
from __future__ import annotations

import os
import ssl
import socket
import datetime
import ipaddress
from typing import Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CERT_FILE = os.path.join(DATA_DIR, "server_cert.pem")
KEY_FILE = os.path.join(DATA_DIR, "server_key.pem")


def get_all_local_ips() -> list[str]:
    """Coleta todos os IPs da máquina local."""
    ips = ["127.0.0.1", "0.0.0.0"]
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.3)
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            if ip and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def generate_self_signed_cert() -> bool:
    """Gera um certificado autoassinado X.509 com SAN para todos os IPs locais."""
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        # 1. Gerar chave privada RSA 2048
        key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        # 2. Subject / Issuer
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "BR"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Minas Gerais"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, "Teofilo Otoni"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Logistica Casa do Campo"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Logistica Casa do Campo Local Server"),
        ])

        # 3. Construir SAN (Subject Alternative Name)
        san_entries = [
            x509.DNSName("localhost"),
            x509.DNSName("logistica.local"),
        ]
        try:
            hname = socket.gethostname()
            if hname:
                san_entries.append(x509.DNSName(hname))
        except Exception:
            pass

        for ip_str in get_all_local_ips():
            try:
                san_entries.append(x509.IPAddress(ipaddress.ip_address(ip_str)))
            except Exception:
                pass

        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))  # 10 anos
            .add_extension(
                x509.SubjectAlternativeName(san_entries),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        # 4. Gravar arquivos PEM
        with open(KEY_FILE, "wb") as f:
            f.write(
                key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )

        with open(CERT_FILE, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

        return True
    except Exception as e:
        print(f"[AVISO SSL] Falha ao gerar certificado autoassinado: {e}")
        return False


def get_ssl_context() -> Optional[ssl.SSLContext]:
    """Obtém ou cria o contexto SSL para o servidor HTTPS."""
    if not os.path.exists(CERT_FILE) or not os.path.exists(KEY_FILE):
        ok = generate_self_signed_cert()
        if not ok:
            return None

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
        return ctx
    except Exception as e:
        print(f"[AVISO SSL] Erro ao carregar certificados SSL: {e}")
        return None
