"""Genera el informe de colaciones en Excel (xlsxwriter)."""
import io

import xlsxwriter
from django.utils import timezone

from backend.apps.reports.service import ReportData


def _local_str(dt, fmt='%d-%m-%Y %H:%M'):
    if dt is None:
        return ''
    d = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return d.strftime(fmt)


def build_excel(data: ReportData, titulo: str) -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {'in_memory': True})

    f_title = wb.add_format({'bold': True, 'font_size': 16, 'font_color': '#3574F0'})
    f_sub = wb.add_format({'font_size': 11, 'font_color': '#6C707E'})
    f_h = wb.add_format({'bold': True, 'bg_color': '#F0F3F8', 'border': 1})
    f_cell = wb.add_format({'border': 1})
    f_num = wb.add_format({'border': 1, 'align': 'right'})
    f_section = wb.add_format({'bold': True, 'font_size': 12})

    # ---- Hoja Resumen ----
    ws = wb.add_worksheet('Resumen')
    ws.set_column('A:A', 32)
    ws.set_column('B:B', 18)

    estacion = data.station.name if data.station else 'Todas las estaciones'
    desde = timezone.localtime(data.date_from).date() if timezone.is_aware(data.date_from) \
        else data.date_from.date()

    ws.write('A1', 'Control de Colaciones — Casino', f_title)
    ws.write('A2', titulo, f_sub)
    ws.write('A3', f'Estación: {estacion}')
    ws.write('A4', f'Período: {desde:%d-%m-%Y} al {data.display_to:%d-%m-%Y}')
    ws.write('A5', f'Generado: {timezone.localtime():%d-%m-%Y %H:%M}')

    row = 7
    ws.write(row, 0, 'Indicador', f_h)
    ws.write(row, 1, 'Valor', f_h)
    resumen = [
        ('Colaciones servidas', data.servidas),
        ('Personas distintas', data.personas_unicas),
        ('Intentos duplicados', data.duplicados),
        ('Visitas (tarjeta)', data.visitas),
        ('No autorizados', data.no_autorizados),
        ('Fuera de turno sin asociar', data.sin_asociar),
    ]
    for i, (label, val) in enumerate(resumen, start=row + 1):
        ws.write(i, 0, label, f_cell)
        ws.write_number(i, 1, val, f_num)

    def sheet_table(name, headers, rows):
        w = wb.add_worksheet(name)
        for c, h in enumerate(headers):
            w.write(0, c, h, f_h)
            w.set_column(c, c, 26 if c == 0 else 16)
        for r, record in enumerate(rows, start=1):
            for c, value in enumerate(record):
                if isinstance(value, (int, float)):
                    w.write_number(r, c, value, f_num)
                else:
                    w.write(r, c, value, f_cell)
        return w

    # ---- Por día ----
    sheet_table('Por día',
                ['Fecha', 'Colaciones'],
                [(f'{d:%d-%m-%Y}', total) for d, total in data.por_dia])

    # ---- Por turno ----
    turno_rows = []
    for r in data.por_turno:
        s = r.shift
        turno_rows.append((s.name, _local_str(s.started_at),
                           _local_str(s.ended_at) or 'en curso',
                           r.en_turno, r.asociadas, r.total))
    sheet_table('Por turno',
                ['Turno', 'Inicio', 'Término', 'En turno', 'Asociadas', 'Total'],
                turno_rows)

    # ---- Por empresa ----
    sheet_table('Por empresa',
                ['Empresa', 'Colaciones'],
                [(emp, total) for emp, total in data.por_empresa])

    # ---- Por persona ----
    sheet_table('Por persona',
                ['Nombre', 'Empresa', 'Colaciones'],
                list(data.por_persona))

    wb.close()
    return buf.getvalue()
