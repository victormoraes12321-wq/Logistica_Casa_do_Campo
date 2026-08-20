from __future__ import annotations

import os
import socket

from app import HOST, PORT, start_server
from app_core.app_factory import create_app
from app_core.compat_layer import runtime_mode

app = create_app()


def _lan_ip_hint() -> str:
    # Nao depende de internet: apenas resolve a interface local ativa.
    for target in (("10.255.255.255", 1), ("192.0.2.1", 1)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.2)
                sock.connect(target)
                ip = str(sock.getsockname()[0] or "").strip()
                if ip and ip != "127.0.0.1":
                    return ip
        except Exception:
            continue
    try:
        ip = str(socket.gethostbyname(socket.gethostname()) or "").strip()
        if ip and ip != "127.0.0.1":
            return ip
    except Exception:
        pass
    return ""


def _is_port_in_use_error(exc: Exception) -> bool:
    text = str(exc or "").strip().lower()
    return (
        "address already in use" in text
        or "only one usage of each socket address" in text
        or "winerror 10048" in text
    )


def _show_port_error(port: int) -> int:
    print(f"[Logistica][ERRO] A porta {port} ja esta em uso por outro processo.", flush=True)
    print("[Logistica][ERRO] Feche a instancia anterior ou altere a porta APP_PORT.", flush=True)
    return 1


def main() -> int:
    mode = runtime_mode()
    host = (os.environ.get("APP_HOST") or os.environ.get("LOGISTICA_HOST") or HOST).strip() or HOST
    try:
        port = int(os.environ.get("APP_PORT") or os.environ.get("LOGISTICA_PORT") or PORT)
    except Exception:
        port = int(PORT)
    print(f"[Logistica] Runtime: {mode} | Bind: {host}:{port}", flush=True)
    print(f"[Logistica] URL local: http://127.0.0.1:{port}", flush=True)
    if host == "0.0.0.0":
        lan_ip = _lan_ip_hint()
        if lan_ip:
            print(f"[Logistica] URL rede:  http://{lan_ip}:{port}", flush=True)
        else:
            print(f"[Logistica] URL rede:  use o IP deste servidor na porta {port}", flush=True)
    print("[Logistica] Pressione Ctrl+C para encerrar.", flush=True)
    if mode == "legacy":
        try:
            start_server(host=host, port=port)
            return 0
        except OSError as exc:
            if _is_port_in_use_error(exc):
                return _show_port_error(port)
            raise

    try:
        from waitress import serve

        serve(app, host=host, port=port, threads=max(8, int(os.environ.get("WAITRESS_THREADS") or 24)))
    except OSError as exc:
        if _is_port_in_use_error(exc):
            return _show_port_error(port)
        raise
    except Exception:
        # Fallback de desenvolvimento quando waitress indisponivel.
        print("[Logistica] Waitress indisponivel. Usando servidor Flask interno.", flush=True)
        try:
            app.run(host=host, port=port, debug=bool(app.config.get("DEBUG")))
        except OSError as exc:
            if _is_port_in_use_error(exc):
                return _show_port_error(port)
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
