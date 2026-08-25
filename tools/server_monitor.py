# -*- coding: utf-8 -*-
"""
Logística Casa do Campo - Monitor de Servidor 24/7 (Watchdog para CMD)
Executa a aplicação Python em subprocesso, exibe status em tempo real no terminal CMD,
registra logs de erros e reinicia o servidor automaticamente em caso de falha.
"""
from __future__ import annotations

import os
import sys
import time
import socket
import datetime
import subprocess
import threading
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# Configurar encodamento de saída no Windows CMD para evitar erros de charmap
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

RUNTIME_LOG = os.path.join(LOGS_DIR, "runtime.log")
ERRORS_LOG = os.path.join(LOGS_DIR, "server_errors.log")

def get_lan_ip() -> str:
    """Retorna o IP local da máquina na rede."""
    for target in (("10.255.255.255", 1), ("192.0.2.1", 1)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.3)
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
    return "IP_DA_REDE"

def safe_print(text: str, flush: bool = True) -> None:
    """Imprime texto de forma segura sem estourar UnicodeEncodeError em CMDs legados."""
    try:
        print(text, flush=flush)
    except UnicodeEncodeError:
        try:
            safe_text = text.encode("ascii", errors="replace").decode("ascii")
            print(safe_text, flush=flush)
        except Exception:
            pass

def log_to_file(filepath: str, message: str) -> None:
    """Escreve mensagem com timestamp no arquivo de log."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(filepath, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{stamp}] {message}\n")
    except Exception:
        pass

def is_port_in_use(port: int) -> bool:
    """Verifica se a porta local já está ocupada por uma socket escutando."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0

def check_http_health(port: int) -> tuple[bool, str]:
    """Realiza requisição GET em /healthz para checar a saúde do servidor."""
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        req = Request(url, headers={"User-Agent": "LogisticaWatchdog/1.0"})
        with urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                return True, "200 OK"
            return False, f"HTTP {resp.status}"
    except HTTPError as e:
        return False, f"HTTP {e.code}"
    except URLError as e:
        return False, f"Conexão recusada ({e.reason})"
    except Exception as e:
        return False, str(e)

def print_header(host: str, port: int, python_exe: str) -> None:
    lan_ip = get_lan_ip()
    safe_print("\n" + "=" * 75)
    safe_print("        LOGÍSTICA CASA DO CAMPO - SERVIDOR EM EXECUÇÃO 24/7")
    safe_print("=" * 75)
    safe_print(f" Executável Python:  {python_exe}")
    safe_print(f" Modo de Rede:       Bind {host}:{port}")
    safe_print(f" Acesso neste PC:    http://localhost:{port}")
    if host == "0.0.0.0":
        safe_print(f" Acesso na Rede:     http://{lan_ip}:{port}")
    safe_print(f" Arquivo de Log:     logs/runtime.log")
    safe_print(f" Log de Erros:       logs/server_errors.log")
    safe_print("-" * 75)
    safe_print(" A janela do CMD permanecerá aberta exibindo o status e alertas.")
    safe_print(" Em caso de queda, o sistema será reiniciado automaticamente.")
    safe_print(" Pressione Ctrl+C para encerrar o servidor.")
    safe_print("=" * 75 + "\n")

def run_server_monitor(host: str = "0.0.0.0", port: int = 3000, restart_delay: int = 5):
    os.environ["APP_HOST"] = host
    os.environ["LOGISTICA_HOST"] = host
    os.environ["APP_PORT"] = str(port)
    os.environ["LOGISTICA_PORT"] = str(port)
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    
    python_exe = sys.executable
    app_script = os.path.join(BASE_DIR, "app.py")

    log_to_file(RUNTIME_LOG, f"Monitor de Servidor iniciado. Host={host} Port={port} Python={python_exe}")
    print_header(host, port, python_exe)

    restart_count = 0
    start_time = time.time()

    while True:
        stop_health_check = threading.Event()
        proc = None

        def health_loop():
            # Aguarda o app subir (5s iniciais)
            time.sleep(5)
            while not stop_health_check.is_set():
                if proc and proc.poll() is not None:
                    break
                ok, status_msg = check_http_health(port)
                now_str = datetime.datetime.now().strftime('%H:%M:%S')
                uptime_sec = int(time.time() - start_time)
                hrs = uptime_sec // 3600
                mins = (uptime_sec % 3600) // 60
                secs = uptime_sec % 60
                uptime_fmt = f"{hrs:02d}h {mins:02d}m {secs:02d}s"

                if ok:
                    safe_print(f"[{now_str}] [STATUS OK] Servidor Ativo | HTTP {status_msg} | Uptime: {uptime_fmt} | Porta {port}")
                else:
                    safe_print(f"[{now_str}] [ALERTA] Teste HTTP na porta {port}: {status_msg}")
                    log_to_file(ERRORS_LOG, f"Alerta Healthcheck HTTP: {status_msg}")

                stop_health_check.wait(15)

        safe_print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] [INICIALIZANDO] Subprocesso app.py (Tentativa #{restart_count + 1})...")
        log_to_file(RUNTIME_LOG, f"Iniciando app.py (Tentativa #{restart_count + 1})")

        recent_logs = []
        try:
            proc = subprocess.Popen(
                [python_exe, app_script],
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1
            )

            health_thread = threading.Thread(target=health_loop, daemon=True)
            health_thread.start()

            if proc.stdout:
                for line in iter(proc.stdout.readline, ""):
                    line_clean = line.rstrip()
                    if line_clean:
                        safe_print(line_clean)
                        recent_logs.append(line_clean)
                        if len(recent_logs) > 30:
                            recent_logs.pop(0)
                        log_to_file(RUNTIME_LOG, line_clean)

            proc.wait()
            exit_code = proc.returncode
            stop_health_check.set()

        except KeyboardInterrupt:
            stop_health_check.set()
            if proc and proc.poll() is None:
                safe_print("\n[ENCERRANDO] Finalizando servidor a pedido do operador...")
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
            safe_print("[INFO] Servidor desligado com sucesso.")
            log_to_file(RUNTIME_LOG, "Servidor desligado com sucesso via KeyboardInterrupt.")
            sys.exit(0)
        except Exception as e:
            stop_health_check.set()
            exit_code = -1
            recent_logs.append(f"Exceção ao executar subprocesso: {e}")

        restart_count += 1
        stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        safe_print("\n" + "!" * 75)
        safe_print(f"[{stamp}] [ERRO CRÍTICO NO SISTEMA] app.py encerrou inesperadamente!")
        safe_print(f"Código de saída (Exit Code): {exit_code}")
        safe_print("-" * 75)
        safe_print("ÚLTIMAS LINHAS DE SAÍDA DO PROCESSO:")
        for err_line in recent_logs[-15:]:
            safe_print(f"  > {err_line}")
        safe_print("-" * 75)
        safe_print(f"O erro foi salvo em: {ERRORS_LOG}")
        safe_print(f"Reiniciando servidor automaticamente em {restart_delay} segundos (Reinício #{restart_count})...")
        safe_print("!" * 75 + "\n")

        log_err_msg = f"Servidor caiu (Exit Code: {exit_code}). Reinício #{restart_count}.\n" + "\n".join(recent_logs[-10:])
        log_to_file(ERRORS_LOG, log_err_msg)

        time.sleep(restart_delay)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Monitor 24/7 Logística Casa do Campo")
    parser.add_argument("--host", default=os.environ.get("LOGISTICA_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LOGISTICA_PORT", "3000")))
    parser.add_argument("--delay", type=int, default=5)
    args = parser.parse_args()
    run_server_monitor(host=args.host, port=args.port, restart_delay=args.delay)
