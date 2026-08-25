from __future__ import annotations

import html
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "auditoria_final_independente_app_android_v2.7.0.md"
OUTPUT = ROOT / "output" / "pdf" / "Auditoria_Final_Independente_App_Android_v2.7.0.pdf"

GREEN = colors.HexColor("#174C3A")
GREEN_LIGHT = colors.HexColor("#E8F3EE")
GOLD = colors.HexColor("#D5A62E")
INK = colors.HexColor("#23312B")
MUTED = colors.HexColor("#60716A")
GRID = colors.HexColor("#C8D7D0")


def clean_text(value: str) -> str:
    return (
        value.replace("\u2011", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2026", "...")
        .replace("\u00a0", " ")
    )


def inline(value: str) -> str:
    value = html.escape(clean_text(value.strip()))
    value = re.sub(r"`([^`]+)`", r'<font name="Courier">\1</font>', value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", value)
    return value


class AuditDocument(BaseDocTemplate):
    def __init__(self, filename: str):
        super().__init__(
            filename,
            pagesize=A4,
            leftMargin=16 * mm,
            rightMargin=16 * mm,
            topMargin=12 * mm,
            bottomMargin=12 * mm,
            title="Auditoria final independente - App Android Logistica Casa do Campo v2.7.0",
            author="Auditoria tecnica automatizada",
        )
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="normal")
        self.addPageTemplates(PageTemplate(id="audit", frames=[frame]))


def styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "AuditTitle", parent=base["Title"], fontName="Helvetica-Bold", fontSize=24,
            leading=29, textColor=GREEN, alignment=TA_LEFT, spaceAfter=8 * mm,
        ),
        "subtitle": ParagraphStyle(
            "AuditSubtitle", parent=base["Normal"], fontName="Helvetica", fontSize=10.5,
            leading=15, textColor=MUTED, spaceAfter=2.5 * mm,
        ),
        "h2": ParagraphStyle(
            "AuditH2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=14.5,
            leading=18, textColor=GREEN, spaceBefore=5 * mm, spaceAfter=2.5 * mm,
        ),
        "body": ParagraphStyle(
            "AuditBody", parent=base["BodyText"], fontName="Helvetica", fontSize=9.0,
            leading=12.7, textColor=INK, alignment=TA_LEFT, spaceAfter=2 * mm,
        ),
        "bullet": ParagraphStyle(
            "AuditBullet", parent=base["BodyText"], fontName="Helvetica", fontSize=8.7,
            leading=12.0, textColor=INK, leftIndent=5 * mm, firstLineIndent=-3.5 * mm,
            spaceAfter=1.4 * mm,
        ),
        "table": ParagraphStyle(
            "AuditTable", parent=base["BodyText"], fontName="Helvetica", fontSize=7.1,
            leading=9.3, textColor=INK,
        ),
        "table_head": ParagraphStyle(
            "AuditTableHead", parent=base["BodyText"], fontName="Helvetica-Bold", fontSize=7.3,
            leading=9.4, textColor=colors.white, alignment=TA_CENTER,
        ),
        "status": ParagraphStyle(
            "AuditStatus", parent=base["BodyText"], fontName="Helvetica-Bold", fontSize=7.2,
            leading=9.2, textColor=GREEN, alignment=TA_CENTER,
        ),
        "status_nr": ParagraphStyle(
            "AuditStatusNR", parent=base["BodyText"], fontName="Helvetica-Bold", fontSize=7.0,
            leading=9.0, textColor=colors.HexColor("#8A5B00"), alignment=TA_CENTER,
        ),
    }


def table_story(rows: list[list[str]], st: dict[str, ParagraphStyle]):
    rendered = []
    for index, row in enumerate(rows):
        cells = []
        for column, value in enumerate(row):
            if index == 0:
                style = st["table_head"]
            elif column == 1:
                style = st["status_nr"] if "NÃO" in value.upper() else st["status"]
            else:
                style = st["table"]
            cells.append(Paragraph(inline(value), style))
        rendered.append(cells)
    table = Table(rendered, colWidths=[37 * mm, 28 * mm, 96 * mm], repeatRows=1, hAlign="LEFT")
    rules = [
        ("BACKGROUND", (0, 0), (-1, 0), GREEN),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.35, GRID),
        ("LEFTPADDING", (0, 0), (-1, -1), 3.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3.5),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]
    for index in range(1, len(rendered)):
        rules.append(("BACKGROUND", (0, index), (-1, index), colors.white if index % 2 else GREEN_LIGHT))
    table.setStyle(TableStyle(rules))
    return table


def build_story(markdown: str):
    st = styles()
    story = []
    lines = markdown.splitlines()
    index = 0
    first_heading = True
    while index < len(lines):
        raw = clean_text(lines[index]).rstrip()
        stripped = raw.strip()
        if not stripped:
            index += 1
            continue
        if stripped.startswith("# "):
            story.append(Spacer(1, 15 * mm))
            story.append(Paragraph(inline(stripped[2:]), st["title"]))
            first_heading = False
        elif stripped.startswith("## "):
            story.append(Paragraph(inline(stripped[3:]), st["h2"]))
        elif stripped.startswith("|"):
            block = []
            while index < len(lines) and clean_text(lines[index]).strip().startswith("|"):
                parts = [part.strip() for part in clean_text(lines[index]).strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{3,}:?", part) for part in parts):
                    block.append(parts)
                index += 1
            if len(block) > 18:
                story.append(PageBreak())
                story.append(table_story(block, st))
            else:
                story.append(table_story(block, st))
                story.append(Spacer(1, 2 * mm))
            continue
        elif re.match(r"^\d+\.\s", stripped):
            number, text = stripped.split(". ", 1)
            story.append(Paragraph(f"<b>{number}.</b> {inline(text)}", st["bullet"]))
        elif stripped.startswith("- "):
            story.append(Paragraph(f"- {inline(stripped[2:])}", st["bullet"]))
        else:
            style = st["subtitle"] if not first_heading and index < 8 else st["body"]
            story.append(Paragraph(inline(stripped.rstrip("  ")), style))
        index += 1
    return story


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    markdown = SOURCE.read_text(encoding="utf-8")
    document = AuditDocument(str(OUTPUT))
    document.build(build_story(markdown))
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
