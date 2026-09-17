"""Genera el informe de colaciones en PDF (reportlab), con el estilo del informe C#."""
import io

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
)

from backend.apps.reports.service import ReportData

ACCENT = colors.HexColor('#3574F0')
MUTED = colors.HexColor('#6C707E')
GREEN = colors.HexColor('#57965C')
AMBER = colors.HexColor('#E8A33D')
RED = colors.HexColor('#DB5C5C')
LINE = colors.HexColor('#E1E4E8')
HEADER_BG = colors.HexColor('#F0F3F8')

DIAS = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']
MESES = ['', 'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
         'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']


def _fecha_larga(d):
    return f'{DIAS[d.weekday()]} {d.day:02d}-{d.month:02d}-{d.year}'


def build_pdf(data: ReportData, titulo: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
        title=titulo,
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle('h1', parent=styles['Title'], fontSize=18,
                        textColor=ACCENT, alignment=0, spaceAfter=2)
    sub = ParagraphStyle('sub', parent=styles['Normal'], fontSize=11, textColor=MUTED)
    meta = ParagraphStyle('meta', parent=styles['Normal'], fontSize=8,
                          textColor=MUTED, alignment=TA_RIGHT)
    section = ParagraphStyle('section', parent=styles['Heading2'], fontSize=13,
                             textColor=colors.HexColor('#1F2328'), spaceBefore=14, spaceAfter=6)
    small = ParagraphStyle('small', parent=styles['Normal'], fontSize=8, textColor=MUTED)

    story = []

    desde = data.date_from
    from django.utils import timezone
    desde_local = timezone.localtime(desde).date() if timezone.is_aware(desde) else desde.date()

    estacion_txt = data.station.name if data.station else 'Todas las estaciones'
    header_tbl = Table([[
        Paragraph(f'Control de Colaciones — Casino<br/>'
                  f'<font size=11 color="#6C707E">{titulo}</font>', h1),
        Paragraph(
            f'Estación: {estacion_txt}<br/>'
            f'Período: {desde_local:%d-%m-%Y} al {data.display_to:%d-%m-%Y}<br/>'
            f'Generado: {timezone.localtime():%d-%m-%Y %H:%M}', meta),
    ]], colWidths=[110 * mm, 70 * mm])
    header_tbl.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LINEBELOW', (0, 0), (-1, -1), 1, ACCENT),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 6 * mm))

    # ---- Resumen (tarjetas) ----
    story.append(Paragraph('Resumen', section))
    stat_style = ParagraphStyle('stat', parent=styles['Normal'], fontSize=18, leading=20)
    lbl_style = ParagraphStyle('lbl', parent=styles['Normal'], fontSize=7, textColor=MUTED)

    def stat(value, label, color_hex):
        return [
            Paragraph(f'<font color="{color_hex}"><b>{value}</b></font>', stat_style),
            Paragraph(label, lbl_style),
        ]

    stats = [
        stat(data.servidas, 'Colaciones servidas', '#3574F0'),
        stat(data.personas_unicas, 'Personas distintas', '#57965C'),
        stat(data.duplicados, 'Intentos duplicados', '#E8A33D'),
        stat(data.visitas, 'Visitas (tarjeta)', '#6B9BFA'),
        stat(data.no_autorizados, 'No autorizados', '#DB5C5C'),
        stat(data.sin_asociar, 'Fuera de turno s/asociar', '#6C707E'),
    ]
    # cada tarjeta es una mini-tabla en una columna
    cards = [[Table([[s[0]], [s[1]]]) for s in stats]]
    stat_tbl = Table(cards, colWidths=[29 * mm] * 6)
    stat_tbl.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.5, LINE),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, LINE),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(stat_tbl)

    def data_table(headers, rows, col_widths, right_cols=()):
        table_data = [headers] + rows
        t = Table(table_data, colWidths=col_widths, repeatRows=1)
        style = [
            ('BACKGROUND', (0, 0), (-1, 0), HEADER_BG),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('LINEBELOW', (0, 0), (-1, -1), 0.4, LINE),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ]
        for c in right_cols:
            style.append(('ALIGN', (c, 0), (c, -1), 'RIGHT'))
        t.setStyle(TableStyle(style))
        return t

    # ---- Por día ----
    if len(data.por_dia) > 1:
        story.append(Paragraph('Colaciones por día', section))
        rows = [[_fecha_larga(d), str(total)] for d, total in data.por_dia]
        story.append(data_table(['Fecha', 'Colaciones'], rows,
                                [120 * mm, 60 * mm], right_cols=[1]))

    # ---- Por turno ----
    if data.por_turno:
        story.append(Paragraph('Colaciones por turno', section))
        story.append(Paragraph(
            f'«Asociadas» = marcaciones fuera de turno atribuidas a este turno '
            f'(hasta {data.grace_minutes} min antes del inicio, o tras el término del anterior).',
            small))
        story.append(Spacer(1, 2 * mm))
        rows = []
        for r in data.por_turno:
            s = r.shift
            ini = _local(s.started_at)
            fin = _local(s.ended_at) if s.ended_at else 'en curso'
            rows.append([s.name, ini, fin, str(r.en_turno), str(r.asociadas), str(r.total)])
        story.append(data_table(
            ['Turno', 'Inicio', 'Término', 'En turno', 'Asociadas', 'Total'],
            rows, [42 * mm, 30 * mm, 30 * mm, 22 * mm, 22 * mm, 20 * mm],
            right_cols=[3, 4, 5]))

    # ---- Por empresa ----
    if data.por_empresa:
        story.append(Paragraph('Colaciones por empresa', section))
        rows = [[emp, str(total)] for emp, total in data.por_empresa]
        story.append(data_table(['Empresa', 'Colaciones'], rows,
                                [135 * mm, 45 * mm], right_cols=[1]))

    # ---- Detalle por persona ----
    if data.por_persona:
        story.append(Paragraph('Detalle por persona', section))
        rows = [[nombre, empresa, str(total)] for nombre, empresa, total in data.por_persona]
        story.append(data_table(['Nombre', 'Empresa', 'Colaciones'], rows,
                                [80 * mm, 70 * mm, 30 * mm], right_cols=[2]))

    doc.build(story, onLaterPages=_footer, onFirstPage=_footer)
    return buf.getvalue()


def _local(dt):
    from django.utils import timezone
    d = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return f'{d.day:02d}-{d.month:02d} {d.hour:02d}:{d.minute:02d}'


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont('Helvetica', 8)
    canvas.setFillColor(MUTED)
    canvas.drawCentredString(letter[0] / 2, 10 * mm, f'Página {doc.page}')
    canvas.restoreState()
