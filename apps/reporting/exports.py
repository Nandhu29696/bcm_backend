"""Render a report `Table` to bytes: Excel (openpyxl), CSV, or PDF (reportlab)."""

from __future__ import annotations

import csv
import datetime as dt
import io

from django.utils import timezone

from apps.reporting.reports import Table

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MIME = "text/csv"
PDF_MIME = "application/pdf"

MIME = {"xlsx": XLSX_MIME, "csv": CSV_MIME, "pdf": PDF_MIME}


def _cell(value):
    """Plain values only: naive local datetimes for Excel, ISO text for CSV/PDF."""
    if isinstance(value, dt.datetime):
        return timezone.localtime(value).replace(tzinfo=None) if timezone.is_aware(value) else value
    return value


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return (
            timezone.localtime(value).strftime("%Y-%m-%d %H:%M")
            if timezone.is_aware(value)
            else value.strftime("%Y-%m-%d %H:%M")
        )
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def to_xlsx(table: Table) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook()
    sheet = book.active
    sheet.title = table.title[:31]
    sheet.append([table.title])
    sheet["A1"].font = Font(bold=True, size=14)
    if table.subtitle:
        sheet.append([table.subtitle])
        sheet["A2"].font = Font(italic=True, color="666666")
    sheet.append([])
    sheet.append(table.columns)
    header_row = sheet.max_row
    for cell in sheet[header_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F3A5F")
        cell.alignment = Alignment(vertical="center")
    for row in table.rows:
        sheet.append([_cell(v) for v in row])
    for index, column in enumerate(table.columns, start=1):
        width = (
            max([len(str(column))] + [len(_text(r[index - 1])) for r in table.rows[:500]])
            if table.rows
            else len(column)
        )
        sheet.column_dimensions[get_column_letter(index)].width = min(max(10, width + 2), 60)
    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)
    sheet.auto_filter.ref = f"A{header_row}:{get_column_letter(len(table.columns))}{sheet.max_row}"
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def to_csv(table: Table) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(table.columns)
    for row in table.rows:
        writer.writerow([_text(v) for v in row])
    return out.getvalue().encode("utf-8-sig")


def to_pdf(table: Table) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, TableStyle
    from reportlab.platypus import Table as PdfTable

    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out,
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=table.title,
        invariant=1,
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle("cell", parent=styles["BodyText"], fontSize=7, leading=8.5)
    head = ParagraphStyle("head", parent=body, textColor=colors.white, fontName="Helvetica-Bold")
    story = [Paragraph(table.title, styles["Title"])]
    if table.subtitle:
        story.append(Paragraph(table.subtitle, styles["Italic"]))
    story.append(Spacer(1, 4 * mm))

    data = [[Paragraph(c, head) for c in table.columns]]
    for row in table.rows:
        data.append(
            [Paragraph(_text(v).replace("&", "&amp;").replace("<", "&lt;"), body) for v in row]
        )
    width = doc.width / max(len(table.columns), 1)
    grid = PdfTable(data, colWidths=[width] * len(table.columns), repeatRows=1)
    grid.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A5F")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B8BEC9")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F5F9")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(grid)
    doc.build(story)
    return out.getvalue()


RENDERERS = {"xlsx": to_xlsx, "csv": to_csv, "pdf": to_pdf}


def render(table: Table, report_format: str) -> tuple[bytes, str]:
    return RENDERERS[report_format](table), MIME[report_format]
