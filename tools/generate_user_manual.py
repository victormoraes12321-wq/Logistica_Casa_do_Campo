# -*- coding: utf-8 -*-
"""Generate the short end-user PDF manual for Logistica Casa do Campo."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "manual_assets"
SCREENSHOTS = ASSETS / "screenshots"
ANNOTATED = ASSETS / "annotated"
DEMO_DB = ASSETS / "manual_demo.sqlite3"
OUTPUT_PDF = ROOT / "Manual_de_Uso_Logistica_Casa_do_Campo.pdf"

RED = HexColor("#D90429")
GREEN = HexColor("#174F2A")
YELLOW = HexColor("#FFBF1F")
INK = HexColor("#243126")
MUTED = HexColor("#667164")
BG = HexColor("#F6F7F2")
LINE = HexColor("#D9DED4")
SOFT_GREEN = HexColor("#EAF3EA")
SOFT_YELLOW = HexColor("#FFF6D7")

PAGE_W, PAGE_H = A4
MARGIN = 42


def register_fonts() -> tuple[str, str]:
    candidates = [
        (Path("C:/Windows/Fonts/segoeui.ttf"), Path("C:/Windows/Fonts/segoeuib.ttf")),
        (Path("C:/Windows/Fonts/calibri.ttf"), Path("C:/Windows/Fonts/calibrib.ttf")),
    ]
    for regular, bold in candidates:
        if regular.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont("ManualBody", str(regular)))
            pdfmetrics.registerFont(TTFont("ManualBold", str(bold)))
            return "ManualBody", "ManualBold"
    return "Helvetica", "Helvetica-Bold"


FONT, FONT_BOLD = register_fonts()


def clean_demo_db() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(DEMO_DB) + suffix)
        if target.exists() and ASSETS in target.parents:
            target.unlink()


def prepare_demo_db() -> None:
    clean_demo_db()
    os.environ["APP_RUNTIME"] = "legacy"
    os.environ["APP_HOST"] = "127.0.0.1"
    os.environ["DATABASE_URL"] = f"sqlite:///{DEMO_DB.as_posix()}"
    os.environ["LOGISTICA_DB_PATH"] = str(DEMO_DB)
    sys.path.insert(0, str(ROOT))
    import app  # noqa: PLC0415

    app.init_db()
    today = date.today()
    now = app.now()

    with app.conn() as db:
        db.execute("UPDATE settings SET value=? WHERE key='company_subtitle'", ("Manual operacional rápido",))
        db.execute("UPDATE settings SET value=? WHERE key='system_name'", ("Logística Casa do Campo",))

        users = [
            ("Ana Faturamento", "ana.fat", "Faturamento"),
            ("Bruno Expedição", "bruno.exp", "Expedicao"),
            ("Carla Consulta", "carla.consulta", "Consulta"),
            ("Diego Motorista", "diego.motorista", "Motorista"),
        ]
        for name, username, role in users:
            db.execute(
                "INSERT OR IGNORE INTO users(name,username,password_hash,role,active,created_at) VALUES(?,?,?,?,1,?)",
                (name, username, app.hash_password("Senha@1234"), role, now),
            )

        db.execute(
            "UPDATE drivers SET name=?,phone=?,document=?,vehicle_default=?,active=1 WHERE id=1",
            ("João Silva", "(11) 98888-1100", "123.456.789-00", "Caminhão Baú 01"),
        )
        db.execute(
            "UPDATE vehicles SET name=?,plate=?,type=?,capacity=?,active=1 WHERE id=1",
            ("Caminhão Baú 01", "ABC1D23", "Baú", "6500"),
        )
        db.execute(
            "INSERT INTO drivers(name,phone,document,vehicle_default,active) VALUES(?,?,?,?,1)",
            ("Marina Souza", "(11) 97777-2200", "987.654.321-00", "Truck 02"),
        )
        db.execute(
            "INSERT INTO vehicles(name,plate,type,capacity,active) VALUES(?,?,?,?,1)",
            ("Truck 02", "DEF4G56", "Truck", "11000"),
        )

        route_cities = [
            ("Rota Oeste", "Capivari", "SP", 1),
            ("Rota Oeste", "Monte Mor", "SP", 2),
            ("Rota Norte", "Limeira", "SP", 1),
            ("Rota Sul", "Itapetininga", "SP", 1),
        ]
        for route_name, city, uf, seq in route_cities:
            db.execute(
                """INSERT INTO route_cities(route_name,city,uf,delivery_order,active,notes,created_at,updated_at)
                   VALUES(?,?,?,?,1,?,?,?)""",
                (route_name, city, uf, seq, "Cadastro demonstrativo do manual", now, now),
            )

        clients = [
            ("Fazenda Boa Vista", "12.345.678/0001-00", "Capivari", "Rota Oeste", "Estrada Municipal, km 12"),
            ("Sítio Primavera", "23.456.789/0001-11", "Monte Mor", "Rota Oeste", "Bairro Primavera"),
            ("Agro Horizonte", "34.567.890/0001-22", "Limeira", "Rota Norte", "Rodovia Anhanguera, km 150"),
            ("Chácara Santa Clara", "45.678.901/0001-33", "Itapetininga", "Rota Sul", "Zona Rural"),
        ]
        client_ids: list[int] = []
        for name, doc, city, route_name, address in clients:
            cur = db.execute(
                """INSERT INTO clients(name,document,phone,whatsapp,city,neighborhood,farm_name,address,reference_point,notes,route_name,active,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (name, doc, "(11) 90000-0000", "", city, "Zona Rural", name, address, "Portão principal", "", route_name, 1, now),
            )
            client_ids.append(cur.lastrowid)

        sale = today - timedelta(days=3)
        deadline = today + timedelta(days=12)
        orders = [
            ("PED-1001", client_ids[0], "Venda", 3250, 820, "Rota Oeste", "Capivari", "Pedido aguardando faturamento"),
            ("PED-1002", client_ids[1], "Faturado", 4890, 1450, "Rota Oeste", "Monte Mor", "Pronto para carregar"),
            ("PED-1003", client_ids[2], "Saiu para entrega", 2790, 620, "Rota Norte", "Limeira", "Em rota"),
            ("PED-1004", client_ids[3], "Acertado", 1980, 400, "Rota Sul", "Itapetininga", "Entrega concluída"),
            ("PED-1005", client_ids[0], "Problema", 740, 110, "Rota Oeste", "Capivari", "Cliente ausente"),
        ]
        order_ids: dict[str, int] = {}
        for number, client_id, status, total, weight, route_name, city, notes in orders:
            cur = db.execute(
                """INSERT INTO orders(order_number,client_id,seller_id,status,urgency,sale_date,expected_delivery_date,payment_method,total_value,weight_kg,
                                      delivery_address,route_name,city,uf,notes,invoice_number,invoiced_at,driver_id,vehicle_id,delivered_at,final_notes,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    number,
                    client_id,
                    1,
                    status,
                    "Normal",
                    sale.isoformat(),
                    deadline.isoformat(),
                    "Boleto",
                    total,
                    weight,
                    "Conforme cadastro do cliente",
                    route_name,
                    city,
                    "SP",
                    notes,
                    f"NF-{number[-4:]}" if status != "Venda" else "",
                    now if status != "Venda" else "",
                    1 if status in ("Saiu para entrega", "Acertado", "Problema") else None,
                    1 if status in ("Saiu para entrega", "Acertado", "Problema") else None,
                    (today - timedelta(days=1)).isoformat() if status in ("Acertado", "Problema") else "",
                    notes if status in ("Acertado", "Problema") else "",
                    now,
                    now,
                ),
            )
            order_ids[number] = cur.lastrowid
            db.execute(
                "INSERT INTO order_items(order_id,product_code,product_name,category,quantity,unit,weight_kg,notes) VALUES(?,?,?,?,?,?,?,?)",
                (cur.lastrowid, "INS-001", "Insumos agrícolas", "Pedido", 10, "saco", weight, ""),
            )
            db.execute(
                "INSERT INTO order_history(order_id,user_id,old_status,new_status,action,notes,created_at) VALUES(?,?,?,?,?,?,?)",
                (cur.lastrowid, 1, "", status, "Criado para manual", notes, now),
            )

        route_id = db.execute(
            """INSERT INTO routes(name,date,driver_id,vehicle_id,status,route_name,total_weight,capacity,notes,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("Carga Oeste - exemplo", today.isoformat(), 1, 1, "Planejada", "Rota Oeste", 2270, 6500, "Carga demonstrativa do manual", now),
        ).lastrowid
        db.execute("INSERT INTO route_orders(route_id,order_id,delivery_order,status) VALUES(?,?,?,?)", (route_id, order_ids["PED-1002"], 1, "Pendente"))
        db.execute("INSERT INTO route_orders(route_id,order_id,delivery_order,status) VALUES(?,?,?,?)", (route_id, order_ids["PED-1003"], 2, "Em rota"))

        db.execute(
            "INSERT INTO delivery_problems(order_id,problem_type,description,created_at) VALUES(?,?,?,?)",
            (order_ids["PED-1005"], "Cliente ausente", "Retornar contato antes de reenviar.", now),
        )
        db.execute(
            "INSERT INTO audit_logs(created_at,user_name,action,module,entity,old_value,new_value,notes) VALUES(?,?,?,?,?,?,?,?)",
            (now, "Administrador GOD", "Preparou dados demonstrativos", "Manual", "Banco temporário", "", "", ""),
        )
        db.commit()


def find_node_exe(explicit: str | None) -> str:
    if explicit:
        return explicit
    found = shutil.which("node")
    if found:
        return found
    bundled = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"
    if bundled.exists():
        return str(bundled)
    raise RuntimeError("Node.js não encontrado para capturar os prints.")


def find_node_modules(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    bundled = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "node_modules"
    return str(bundled) if bundled.exists() else None


def wait_health(base_url: str, proc: subprocess.Popen, timeout: int = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Servidor encerrou antes de responder ao healthcheck.")
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("Servidor não respondeu ao healthcheck dentro do prazo.")


def capture_screenshots(port: int, node_exe: str | None, node_modules: str | None) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    for png in SCREENSHOTS.glob("*.png"):
        png.unlink()

    env = os.environ.copy()
    env["APP_RUNTIME"] = "legacy"
    env["APP_HOST"] = "127.0.0.1"
    env["APP_PORT"] = str(port)
    env["DATABASE_URL"] = f"sqlite:///{DEMO_DB.as_posix()}"
    env["LOGISTICA_DB_PATH"] = str(DEMO_DB)
    env["LOGISTICA_HOST"] = "127.0.0.1"
    env["LOGISTICA_PORT"] = str(port)
    env["LOGISTICA_MAX_WORKERS"] = "20"

    server_log = ASSETS / "manual_server.log"
    server_err = ASSETS / "manual_server.err.log"
    with server_log.open("w", encoding="utf-8") as out, server_err.open("w", encoding="utf-8") as err:
        proc = subprocess.Popen([sys.executable, str(ROOT / "app.py")], cwd=str(ROOT), env=env, stdout=out, stderr=err)
        try:
            base_url = f"http://127.0.0.1:{port}"
            wait_health(base_url, proc)

            node_env = os.environ.copy()
            modules = find_node_modules(node_modules)
            if modules:
                module_paths = [modules]
                pnpm_modules = Path(modules) / ".pnpm" / "node_modules"
                if pnpm_modules.exists():
                    module_paths.append(str(pnpm_modules))
                if node_env.get("NODE_PATH"):
                    module_paths.append(node_env["NODE_PATH"])
                node_env["NODE_PATH"] = os.pathsep.join(module_paths)
            subprocess.run(
                [
                    find_node_exe(node_exe),
                    str(ROOT / "tools" / "capture_manual_screenshots.cjs"),
                    "--base-url",
                    base_url,
                    "--out",
                    str(SCREENSHOTS),
                ],
                cwd=str(ROOT),
                env=node_env,
                check=True,
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def pil_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/segoeuib.ttf") if bold else Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/calibrib.ttf") if bold else Path("C:/Windows/Fonts/calibri.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


CALLOUTS = {
    "01_login": [(0.72, 0.50, "1"), (0.72, 0.59, "2"), (0.72, 0.68, "3")],
    "02_dashboard": [(0.13, 0.32, "1"), (0.52, 0.11, "2"), (0.86, 0.11, "3")],
    "03_orders": [(0.13, 0.25, "1"), (0.45, 0.12, "2"), (0.87, 0.28, "3")],
    "04_order_new": [(0.29, 0.38, "1"), (0.72, 0.39, "2"), (0.20, 0.84, "3")],
    "05_billing": [(0.13, 0.29, "1"), (0.48, 0.30, "2"), (0.88, 0.43, "3")],
    "06_routes": [(0.13, 0.34, "1"), (0.47, 0.12, "2"), (0.82, 0.35, "3")],
    "07_route_detail": [(0.45, 0.36, "1"), (0.82, 0.34, "2"), (0.55, 0.76, "3")],
    "08_catalog": [(0.42, 0.35, "1"), (0.84, 0.34, "2"), (0.83, 0.52, "3")],
    "09_settings": [(0.26, 0.36, "1"), (0.60, 0.52, "2"), (0.83, 0.74, "3")],
    "10_backup": [(0.36, 0.36, "1"), (0.48, 0.57, "2"), (0.66, 0.78, "3")],
}


def annotate_screenshots() -> None:
    ANNOTATED.mkdir(parents=True, exist_ok=True)
    for src in SCREENSHOTS.glob("*.png"):
        key = src.stem
        with Image.open(src).convert("RGB") as image:
            draw = ImageDraw.Draw(image)
            font = pil_font(34, bold=True)
            for x_rel, y_rel, label in CALLOUTS.get(key, []):
                x, y = int(image.width * x_rel), int(image.height * y_rel)
                radius = 27
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(217, 4, 41), outline=(255, 255, 255), width=5)
                bbox = draw.textbbox((0, 0), label, font=font)
                draw.text((x - (bbox[2] - bbox[0]) / 2, y - (bbox[3] - bbox[1]) / 2 - 2), label, fill=(255, 255, 255), font=font)
            out = ANNOTATED / f"{key}.jpg"
            image.save(out, "JPEG", quality=88, optimize=True)


def draw_wrapped(c: canvas.Canvas, text: str, x: float, y: float, width: float, size: float = 10, leading: float = 13, color=INK, font: str = FONT) -> float:
    c.setFillColor(color)
    c.setFont(font, size)
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            y -= leading * 0.55
            continue
        line = ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if pdfmetrics.stringWidth(candidate, font, size) <= width:
                line = candidate
            else:
                c.drawString(x, y, line)
                y -= leading
                line = word
        if line:
            c.drawString(x, y, line)
            y -= leading
    return y


def draw_header(c: canvas.Canvas, title: str, page_num: int) -> None:
    c.setFillColor(GREEN)
    c.rect(0, PAGE_H - 46, PAGE_W, 46, fill=1, stroke=0)
    logo = ROOT / "static" / "logo.png"
    if logo.exists():
        c.drawImage(str(logo), MARGIN, PAGE_H - 38, 28, 28, preserveAspectRatio=True, mask="auto")
    c.setFillColor(white)
    c.setFont(FONT_BOLD, 10)
    c.drawString(MARGIN + 38, PAGE_H - 28, "Logística Casa do Campo")
    c.setFont(FONT, 8.5)
    c.drawRightString(PAGE_W - MARGIN, PAGE_H - 28, title)
    c.setFillColor(MUTED)
    c.setFont(FONT, 8)
    c.drawRightString(PAGE_W - MARGIN, 24, f"Manual rápido de uso | página {page_num}")


def draw_title(c: canvas.Canvas, text: str, y: float) -> float:
    c.setFillColor(INK)
    c.setFont(FONT_BOLD, 22)
    c.drawString(MARGIN, y, text)
    c.setFillColor(YELLOW)
    c.rect(MARGIN, y - 10, 72, 4, fill=1, stroke=0)
    return y - 28


def draw_card(c: canvas.Canvas, x: float, y: float, w: float, h: float, title: str, body: str, fill=SOFT_GREEN) -> None:
    c.setFillColor(fill)
    c.setStrokeColor(LINE)
    c.roundRect(x, y - h, w, h, 8, fill=1, stroke=1)
    c.setFillColor(GREEN)
    c.setFont(FONT_BOLD, 10)
    c.drawString(x + 12, y - 20, title)
    draw_wrapped(c, body, x + 12, y - 38, w - 24, size=8.7, leading=11, color=INK)


def draw_steps(c: canvas.Canvas, steps: list[str], x: float, y: float, width: float, size: float = 9.5) -> float:
    for index, step in enumerate(steps, start=1):
        c.setFillColor(RED)
        c.circle(x + 8, y - 4, 8, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont(FONT_BOLD, 8)
        c.drawCentredString(x + 8, y - 7, str(index))
        y = draw_wrapped(c, step, x + 24, y, width - 24, size=size, leading=size + 3)
        y -= 4
    return y


def draw_bullets(c: canvas.Canvas, bullets: list[str], x: float, y: float, width: float, size: float = 9.4) -> float:
    for item in bullets:
        c.setFillColor(YELLOW)
        c.circle(x + 4, y - 4, 3, fill=1, stroke=0)
        y = draw_wrapped(c, item, x + 15, y, width - 15, size=size, leading=size + 3)
        y -= 3
    return y


def draw_image_fit(c: canvas.Canvas, img_path: Path, x: float, y_top: float, max_w: float, max_h: float) -> float:
    with Image.open(img_path) as im:
        iw, ih = im.size
    scale = min(max_w / iw, max_h / ih)
    w, h = iw * scale, ih * scale
    c.setFillColor(white)
    c.setStrokeColor(LINE)
    c.roundRect(x - 5, y_top - h - 5, w + 10, h + 10, 8, fill=1, stroke=1)
    c.drawImage(ImageReader(str(img_path)), x, y_top - h, width=w, height=h)
    return y_top - h - 18


def add_page(c: canvas.Canvas, page_num: int, header: str, title: str) -> float:
    c.showPage()
    c.setFillColor(BG)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    draw_header(c, header, page_num)
    return draw_title(c, title, PAGE_H - 82)


def image(name: str) -> Path:
    return ANNOTATED / f"{name}.jpg"


def build_pdf(out_pdf: Path) -> None:
    annotate_screenshots()
    c = canvas.Canvas(str(out_pdf), pagesize=A4)
    c.setTitle("Manual de Uso - Logística Casa do Campo")
    c.setAuthor("Casa do Campo")

    c.setFillColor(BG)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    c.setFillColor(GREEN)
    c.rect(0, PAGE_H - 185, PAGE_W, 185, fill=1, stroke=0)
    c.setFillColor(YELLOW)
    c.rect(0, PAGE_H - 190, PAGE_W, 8, fill=1, stroke=0)
    logo = ROOT / "static" / "logo.png"
    if logo.exists():
        c.drawImage(str(logo), MARGIN, PAGE_H - 135, 78, 78, preserveAspectRatio=True, mask="auto")
    c.setFillColor(white)
    c.setFont(FONT_BOLD, 30)
    c.drawString(MARGIN + 98, PAGE_H - 82, "Manual de Uso")
    c.setFont(FONT, 18)
    c.drawString(MARGIN + 98, PAGE_H - 112, "Logística Casa do Campo")
    c.setFont(FONT, 11)
    c.drawString(MARGIN + 98, PAGE_H - 142, "Guia rápido para operação local, multiusuário e em rede")

    y = PAGE_H - 238
    y = draw_wrapped(c, "Este manual foi feito para quem vai usar o sistema no dia a dia: faturamento, expedição, gestores, motoristas e administradores. Ele mostra onde clicar, o que cada tela resolve e como agir quando o sistema bloquear uma ação.", MARGIN, y, PAGE_W - 2 * MARGIN, size=11.2, leading=15)
    draw_card(c, MARGIN, y - 16, 244, 94, "O que guardar na cabeça", "O fluxo principal é: pedido criado, faturado, colocado em carga, entregue e acertado. Se o sistema bloquear algo, leia a mensagem: ela indica a ação correta.", SOFT_GREEN)
    draw_card(c, MARGIN + 266, y - 16, 244, 94, "Uso no servidor local", "No servidor, deixe o modo Network com watchdog. Nos outros computadores, acesse pelo IP do servidor na porta 3000.", SOFT_YELLOW)
    y -= 140
    c.setFont(FONT_BOLD, 11)
    c.setFillColor(INK)
    c.drawString(MARGIN, y, "Informações rápidas")
    y -= 20
    y = draw_bullets(c, [
        "Endereço no servidor: http://127.0.0.1:3000",
        "Endereço nos computadores da rede: http://IP_DO_SERVIDOR:3000",
        "Login inicial criado automaticamente: usuário admin, senha admin123. Troque a senha no primeiro uso.",
        f"Versão deste manual: {date.today().strftime('%d/%m/%Y')}",
    ], MARGIN, y, PAGE_W - 2 * MARGIN, size=10)

    y = add_page(c, 2, "Acesso e login", "1. Acesso e login")
    y = draw_image_fit(c, image("01_login"), MARGIN, y, PAGE_W - 2 * MARGIN, 292)
    draw_card(c, MARGIN, y, 244, 78, "Local", "Use no próprio servidor. Bom para manutenção, testes e operação feita diretamente na máquina principal.", SOFT_GREEN)
    draw_card(c, MARGIN + 266, y, 244, 78, "Network", "Use para liberar acesso aos computadores da mesma rede. É o modo indicado para a operação da empresa.", SOFT_YELLOW)
    y -= 110
    y = draw_steps(c, [
        "Abra o endereço do sistema no navegador.",
        "Digite seu usuário e sua senha. Cada funcionário deve usar o próprio login.",
        "Clique em Entrar no sistema. Se aparecer bloqueio de senha, aguarde o tempo indicado ou peça reset ao administrador.",
    ], MARGIN, y, PAGE_W - 2 * MARGIN)

    y = add_page(c, 3, "Painel inicial", "2. Painel e navegação")
    y = draw_image_fit(c, image("02_dashboard"), MARGIN, y, PAGE_W - 2 * MARGIN, 302)
    y = draw_bullets(c, [
        "Menu lateral: mostra apenas as áreas que seu perfil pode acessar.",
        "Busca superior: localiza pedido, nota fiscal, cliente, fazenda, cidade ou rota.",
        "Indicadores do painel: acompanhe pendências, atrasos, cargas e gargalos antes de começar o dia.",
        "Botão Imprimir: útil para reunião rápida, conferência ou operação sem tela.",
    ], MARGIN, y, PAGE_W - 2 * MARGIN)
    draw_card(c, MARGIN, y - 8, PAGE_W - 2 * MARGIN, 58, "Rotina sugerida", "Ao abrir o sistema, confira atrasos no painel, revise pedidos faturados, monte cargas, acompanhe entregas e gere backup ao final de mudanças importantes.", SOFT_GREEN)

    y = add_page(c, 4, "Pedidos", "3. Pedidos e cadastro")
    c.setFont(FONT_BOLD, 12)
    c.setFillColor(INK)
    c.drawString(MARGIN, y, "Lista de pedidos")
    c.drawString(MARGIN + 263, y, "Novo pedido")
    y -= 10
    y_left = draw_image_fit(c, image("03_orders"), MARGIN, y, 242, 170)
    y_right = draw_image_fit(c, image("04_order_new"), MARGIN + 263, y, 242, 170)
    y = min(y_left, y_right) - 8
    y = draw_steps(c, [
        "Clique em Novo Pedido quando uma venda precisar entrar na logística.",
        "Preencha cliente, cidade, rota, peso, valor, endereço e prazo. Campos vazios ou dados inválidos são bloqueados.",
        "Use Editar para corrigir dados. Use Cancelar/Reabrir quando o pedido mudar de situação operacional.",
    ], MARGIN, y, PAGE_W - 2 * MARGIN)
    draw_card(c, MARGIN, y - 5, PAGE_W - 2 * MARGIN, 72, "Fluxo de status", "Venda: pedido criado. Faturado: pronto para expedição. Saiu para entrega: está em carga. Acertado: entrega finalizada. Problema ou Cancelado: exige motivo e fica registrado.", SOFT_YELLOW)

    y = add_page(c, 5, "Faturamento", "4. Faturamento")
    y = draw_image_fit(c, image("05_billing"), MARGIN, y, PAGE_W - 2 * MARGIN, 294)
    y = draw_steps(c, [
        "Entre em Faturamento e localize pedidos em Venda.",
        "Informe nota fiscal e dados exigidos. O sistema impede faturar o mesmo pedido duas vezes.",
        "Depois de faturado, o pedido fica disponível para ser incluído em carga pela expedição.",
    ], MARGIN, y, PAGE_W - 2 * MARGIN)
    y = draw_bullets(c, [
        "Se a tela não aparecer no menu, seu usuário não tem permissão de faturamento.",
        "Se a nota fiscal já existir em outro pedido, o sistema avisa e bloqueia a duplicidade.",
        "Erros de preenchimento aparecem em mensagem clara na tela, sem códigos técnicos.",
    ], MARGIN, y - 4, PAGE_W - 2 * MARGIN)

    y = add_page(c, 6, "Cargas e rotas", "5. Cargas, rotas e acerto")
    c.setFont(FONT_BOLD, 12)
    c.setFillColor(INK)
    c.drawString(MARGIN, y, "Cargas")
    c.drawString(MARGIN + 263, y, "Detalhe da carga")
    y -= 10
    y_left = draw_image_fit(c, image("06_routes"), MARGIN, y, 242, 170)
    y_right = draw_image_fit(c, image("07_route_detail"), MARGIN + 263, y, 242, 170)
    y = min(y_left, y_right) - 8
    y = draw_steps(c, [
        "Crie a carga escolhendo motorista, veículo, rota e data.",
        "Adicione apenas pedidos faturados. O sistema bloqueia pedido cancelado, duplicado em outra carga ativa ou acima da capacidade do veículo.",
        "Ajuste a sequência de entrega, acompanhe status e faça o acerto quando a rota voltar.",
        "Se houver problema, registre a ocorrência no pedido para manter histórico.",
    ], MARGIN, y, PAGE_W - 2 * MARGIN)

    y = add_page(c, 7, "Cadastros base", "6. Clientes, cidades, rotas e veículos")
    y = draw_image_fit(c, image("08_catalog"), MARGIN, y, PAGE_W - 2 * MARGIN, 292)
    y = draw_bullets(c, [
        "Clientes: cadastre nome, contato, cidade, endereço, fazenda e rota preferencial.",
        "Motoristas e veículos: mantenha ativos apenas os que podem ser usados em cargas.",
        "Cidades/rotas-base: cadastre vínculos de rota, cidade, UF e ordem de entrega.",
        "Editar altera o cadastro para usos futuros. Inativar impede novo uso sem quebrar histórico antigo.",
        "O sistema bloqueia duplicidades como rota/cidade repetida, usuário repetido e placa repetida.",
    ], MARGIN, y, PAGE_W - 2 * MARGIN)
    draw_card(c, MARGIN, y - 6, PAGE_W - 2 * MARGIN, 58, "Regra importante", "Evite apagar informações operacionais. Quando houver pedido, carga ou histórico vinculado, prefira inativar para preservar rastreabilidade.", SOFT_YELLOW)

    y = add_page(c, 8, "Administrador", "7. Usuários e permissões")
    y = draw_image_fit(c, image("09_settings"), MARGIN, y, PAGE_W - 2 * MARGIN, 292)
    y = draw_bullets(c, [
        "Crie usuários pelo painel Configurações. Cada pessoa deve ter login individual.",
        "Defina o perfil conforme a função: GOD, Admin, Gestor, Faturamento, Expedição, Motorista, Operador ou Consulta.",
        "Use Resetar senha quando o funcionário esquecer o acesso. Não informe senha de outro usuário.",
        "Permissões podem ser ajustadas por perfil ou por usuário. O backend também bloqueia acesso digitando URL direta.",
        "Não é permitido desativar o próprio usuário logado nem remover o último GOD ativo.",
    ], MARGIN, y, PAGE_W - 2 * MARGIN)

    y = add_page(c, 9, "Backup e dúvidas", "8. Backup, erros e checklist")
    y = draw_image_fit(c, image("10_backup"), MARGIN, y, PAGE_W - 2 * MARGIN, 250)
    c.setFont(FONT_BOLD, 12)
    c.setFillColor(INK)
    c.drawString(MARGIN, y, "Mensagens comuns")
    y -= 18
    y = draw_bullets(c, [
        "Sem permissão: peça ao administrador para revisar seu perfil ou liberação específica.",
        "Registro vinculado: existe pedido, carga ou histórico usando esse cadastro. Inative ou finalize vínculos antes.",
        "Capacidade excedida: reduza pedidos da carga ou escolha veículo com capacidade maior.",
        "Pedido já mudou de status: outra pessoa alterou antes de você. Atualize a tela e confira a situação atual.",
    ], MARGIN, y, PAGE_W - 2 * MARGIN, size=8.9)
    draw_card(c, MARGIN, y - 4, 244, 112, "Checklist de abertura", "Entrar no sistema. Conferir dashboard. Revisar pedidos em atraso. Confirmar pedidos faturados. Montar cargas do dia.", SOFT_GREEN)
    draw_card(c, MARGIN + 266, y - 4, 244, 112, "Checklist de fechamento", "Registrar entregas e problemas. Fazer acerto de carga. Conferir pendências. Gerar backup. Fechar o navegador em computadores compartilhados.", SOFT_YELLOW)

    c.save()


def validate_pdf(path: Path) -> int:
    from pypdf import PdfReader  # noqa: PLC0415

    reader = PdfReader(str(path))
    return len(reader.pages)


def main() -> int:
    parser = argparse.ArgumentParser(description="Gera o manual PDF de uso do sistema.")
    parser.add_argument("--port", type=int, default=3097)
    parser.add_argument("--skip-capture", action="store_true", help="Usa screenshots já existentes.")
    parser.add_argument("--node-exe", default=None)
    parser.add_argument("--node-modules", default=None)
    parser.add_argument("--out", default=str(OUTPUT_PDF))
    args = parser.parse_args()

    prepare_demo_db()
    if not args.skip_capture:
        capture_screenshots(args.port, args.node_exe, args.node_modules)

    missing = [name for name in CALLOUTS if not (SCREENSHOTS / f"{name}.png").exists()]
    if missing:
        raise RuntimeError(f"Prints ausentes: {', '.join(missing)}")

    out_pdf = Path(args.out).resolve()
    build_pdf(out_pdf)
    pages = validate_pdf(out_pdf)
    print(f"PDF gerado: {out_pdf}")
    print(f"Páginas: {pages}")
    print(f"Tamanho: {out_pdf.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
