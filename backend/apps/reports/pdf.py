"""Genera el informe de colaciones en PDF (reportlab), con el estilo del informe C#."""
import io

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Image as RLImage, Paragraph, Spacer, Table, TableStyle,
)

from backend.apps.reports.service import ReportData

# Paleta de la marca jemo: rojo + neutros. Verde y ámbar solo para estados.
JEMO = colors.HexColor('#C8102E')
INK = colors.HexColor('#1D1D1F')
MUTED = colors.HexColor('#6E6E73')
GREEN = colors.HexColor('#1A7F64')
AMBER = colors.HexColor('#9B6829')
LINE = colors.HexColor('#E5E5EA')
HEADER_BG = colors.HexColor('#F2F2F4')

DIAS = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']
MESES = ['', 'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
         'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']


def _fecha_larga(d):
    return f'{DIAS[d.weekday()]} {d.day:02d}-{d.month:02d}-{d.year}'


def _brand_logo():
    """Logo jemo (PNG) para el encabezado; None si el asset no está disponible."""
    try:
        from django.conf import settings
        path = settings.BASE_DIR / 'backend' / 'static' / 'img' / 'jemo-logo.png'
        if not path.exists():
            return None
        w = 40 * mm
        img = RLImage(str(path), width=w, height=w * 93 / 400)  # logo nativo 400x93
        img.hAlign = 'LEFT'
        return img
    except Exception:
        return None


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
                        textColor=INK, alignment=0, spaceAfter=2)
    sub = ParagraphStyle('sub', parent=styles['Normal'], fontSize=11, textColor=MUTED)
    meta = ParagraphStyle('meta', parent=styles['Normal'], fontSize=8,
                          textColor=MUTED, alignment=TA_RIGHT)
    section = ParagraphStyle('section', parent=styles['Heading2'], fontSize=13,
                             textColor=INK, spaceBefore=14, spaceAfter=6)
    small = ParagraphStyle('small', parent=styles['Normal'], fontSize=8, textColor=MUTED)

    story = []

    desde = data.date_from
    from django.utils import timezone
    desde_local = timezone.localtime(desde).date() if timezone.is_aware(desde) else desde.date()

    estacion_txt = data.station.name if data.station else 'Todas las estaciones'
    logo = _brand_logo()
    left_cell = [] if logo is None else [logo, Spacer(1, 3)]
    if logo is None:
        left_cell.append(Paragraph('<font size=24 color="#C8102E"><b><i>jemo</i></b></font>', h1))
    left_cell.append(Paragraph(
        'Control de Colaciones — Casino<br/>'
        f'<font size=11 color="#6E6E73">{titulo}</font>', h1))
    header_tbl = Table([[
        left_cell,
        Paragraph(
            f'Estación: {estacion_txt}<br/>'
            f'Período: {desde_local:%d-%m-%Y} al {data.display_to:%d-%m-%Y}<br/>'
            f'Generado: {timezone.localtime():%d-%m-%Y %H:%M}', meta),
    ]], colWidths=[110 * mm, 70 * mm])
    header_tbl.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LINEBELOW', (0, 0), (-1, -1), 1, JEMO),
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
        stat(data.servidas, 'Colaciones servidas', '#1D1D1F'),
        stat(data.personas_unicas, 'Personas distintas', '#1A7F64'),
        stat(data.duplicados, 'Intentos duplicados', '#9B6829'),
        stat(data.visitas, 'Visitas (tarjeta)', '#3A3A3C'),
        stat(data.manuales, 'Ingreso manual', '#3A3A3C'),
        stat(data.no_autorizados, 'No autorizados', '#C8102E'),
        stat(data.sin_asociar, 'Fuera de turno s/asociar', '#6E6E73'),
    ]
    # cada tarjeta es una mini-tabla en una columna
    cards = [[Table([[s[0]], [s[1]]]) for s in stats]]
    stat_tbl = Table(cards, colWidths=[180 * mm / len(stats)] * len(stats))
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
            f'(hasta {data.grace_minutes} min antes del inicio, o tras el término del anterior). '
            f'«Manual» = cantidad registrada por la cocinera en un turno sin marcación.',
            small))
        story.append(Spacer(1, 2 * mm))
        rows = []
        for r in data.por_turno:
            s = r.shift
            ini = _local(s.started_at)
            if s.is_manual_entry:
                fin = 'ingreso manual'
            else:
                fin = _local(s.ended_at) if s.ended_at else 'en curso'
            rows.append([s.name, ini, fin, str(r.en_turno), str(r.asociadas),
                         str(r.manuales), str(r.total)])
        story.append(data_table(
            ['Turno', 'Inicio', 'Término', 'En turno', 'Asociadas', 'Manual', 'Total'],
            rows, [38 * mm, 26 * mm, 28 * mm, 20 * mm, 22 * mm, 18 * mm, 18 * mm],
            right_cols=[3, 4, 5, 6]))

    meal_types = data.meal_types
    n = len(meal_types)

    # ---- Por empresa (con composición por turno) ----
    if data.por_empresa:
        story.append(Paragraph('Colaciones por empresa', section))
        if meal_types:
            story.append(Paragraph(
                'Cada colación se cuenta en el turno donde se sirvió; el total es la suma de la fila.',
                small))
            story.append(Spacer(1, 2 * mm))
        headers = ['Empresa'] + meal_types + ['Total']
        rows = [[emp] + [str(c) if c else '·' for c in comp] + [str(total)]
                for emp, total, comp in data.por_empresa]
        meal_w, total_w = 18 * mm, 20 * mm
        first_w = 186 * mm - total_w - n * meal_w
        col_widths = [first_w] + [meal_w] * n + [total_w]
        story.append(data_table(headers, rows, col_widths,
                                right_cols=list(range(1, n + 2))))

    # ---- Detalle por persona (con composición por turno) ----
    if data.por_persona:
        story.append(Paragraph('Detalle por persona', section))
        if meal_types:
            nota = 'Composición de las colaciones de cada persona por turno.'
            if data.manuales:
                nota += (f' No incluye las {data.manuales} colaciones de ingreso manual '
                         '(no tienen marcación personal).')
            story.append(Paragraph(nota, small))
            story.append(Spacer(1, 2 * mm))
        headers = ['Nombre', 'Empresa'] + meal_types + ['Total']
        rows = [[nombre, empresa] + [str(c) if c else '·' for c in comp] + [str(total)]
                for nombre, empresa, total, comp in data.por_persona]
        meal_w, total_w = 19 * mm, 18 * mm
        remaining = 186 * mm - total_w - n * meal_w
        col_widths = [remaining * 0.56, remaining * 0.44] + [meal_w] * n + [total_w]
        story.append(data_table(headers, rows, col_widths,
                                right_cols=list(range(2, n + 3))))

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
