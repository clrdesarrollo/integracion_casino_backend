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

    f_title = wb.add_format({'bold': True, 'font_size': 16, 'font_color': '#C8102E'})
    f_sub = wb.add_format({'font_size': 11, 'font_color': '#6E6E73'})
    f_h = wb.add_format({'bold': True, 'bg_color': '#F2F2F4', 'border': 1})
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
        ('Ingreso manual', data.manuales),
        ('No autorizados', data.no_autorizados),
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
        fin = 'ingreso manual' if s.is_manual_entry else (_local_str(s.ended_at) or 'en curso')
        # cómo se abrió y se cerró: por horario (auto) o desde la pantalla del terminal (manual)
        modo_ini = '' if s.is_manual_entry else s.start_mode
        modo_fin = '' if s.is_manual_entry else s.end_mode
        turno_rows.append((s.name, _local_str(s.started_at), modo_ini, fin, modo_fin,
                           r.en_turno, r.manuales, r.total))
    sheet_table('Por turno',
                ['Turno', 'Inicio', 'Modo inicio', 'Término', 'Modo término',
                 'En turno', 'Manual', 'Total'],
                turno_rows)

    # ---- Por empresa (con composición por turno) ----
    sheet_table('Por empresa',
                ['Empresa'] + data.meal_types + ['Total'],
                [(emp, *comp, total) for emp, total, comp in data.por_empresa])

    # ---- Por persona (con composición por turno) ----
    sheet_table('Por persona',
                ['Nombre', 'Empresa'] + data.meal_types + ['Total'],
                [(nombre, empresa, *comp, total)
                 for nombre, empresa, total, comp in data.por_persona])

    # ---- Visitas (registro de entrega de tarjetas) ----
    w = sheet_table('Visitas',
                    ['Entregada', 'Visita', 'RUT / documento', 'Procedencia', 'Viene a ver a',
                     'Empresa visitada', 'Tarjeta', 'Nº tarjeta', 'Entregó', 'Devuelta',
                     'Recibió', 'Estación', 'Colaciones', 'Observación'],
                    [(_local_str(r.delivered_at) if r.registrada else
                      f'{_local_str(r.first_at)} (sin registro; primera colación)',
                      r.visitor_name,
                      r.visitor_document,
                      r.visitor_company,
                      r.visit.host_name if r.registrada else '',
                      r.visit.host_company if r.registrada else '',
                      r.card_display,
                      r.card_no,
                      r.delivered_by_name,
                      (_local_str(r.returned_at) or 'en uso') if r.registrada else '',
                      r.visit.returned_by_name if r.registrada else '',
                      r.station.name if r.station else '',
                      r.colaciones,
                      r.visit.notes if r.registrada else '')
                     for r in data.visitas_detalle])
    w.set_column(0, 0, 30)
    w.set_column(1, 1, 26)
    w.set_column(4, 5, 24)
    w.set_column(13, 13, 40)

    # ---- No autorizados (con el motivo) ----
    w = sheet_table('No autorizados',
                    ['Fecha', 'Nombre', 'Nº empleado / tarjeta', 'Empresa', 'Estación',
                     'Turno', 'Visita', 'Motivo'],
                    [(_local_str(e.event_time),
                      e.person_name or '(desconocido)',
                      (e.card_no if e.is_visitor and e.card_no else e.employee_no) or '',
                      e.company or '',
                      e.station.name,
                      e.shift.name if e.shift else '',
                      'sí' if e.is_visitor else '',
                      e.detail or '')
                     for e in data.no_autorizados_detalle])
    w.set_column(0, 0, 17)
    w.set_column(7, 7, 60)

    wb.close()
    return buf.getvalue()


def build_shift_excel(shift, events) -> bytes:
    """Detalle de UN turno: cómo se abrió y cerró, y cada marcación con su hora al segundo."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {'in_memory': True})
    f_title = wb.add_format({'bold': True, 'font_size': 16, 'font_color': '#C8102E'})
    f_h = wb.add_format({'bold': True, 'bg_color': '#F2F2F4', 'border': 1})
    f_cell = wb.add_format({'border': 1})
    f_num = wb.add_format({'border': 1, 'align': 'right'})

    ws = wb.add_worksheet('Turno')
    ws.set_column('A:A', 28)
    ws.set_column('B:B', 34)
    hora = '%d-%m-%Y %H:%M:%S'
    ws.write('A1', f'Turno {shift.name}', f_title)
    ficha = [
        ('Estación', shift.station.name),
        ('Día de servicio', f'{shift.service_date:%d-%m-%Y}' if shift.service_date else ''),
        ('Inicio', _local_str(shift.started_at, hora)),
        ('Modo de inicio', '' if shift.is_manual_entry else shift.start_mode),
        ('Término', _local_str(shift.ended_at, hora) or 'en curso'),
        ('Modo de término', '' if shift.is_manual_entry else shift.end_mode),
        ('Motivo de término', shift.get_end_reason_display()),
        ('Reapertura', 'sí' if shift.is_reopening else 'no'),
    ]
    if shift.is_manual_entry:
        ficha.append(('Colaciones (ingreso manual)', shift.manual_count))
    else:
        ficha.append(('Colaciones válidas', sum(1 for e in events if e.status == 'Ok')))
        ficha.append(('Marcaciones', len(events)))
    for i, (label, value) in enumerate(ficha, start=2):
        ws.write(i, 0, label, f_h)
        if isinstance(value, int):
            ws.write_number(i, 1, value, f_num)
        else:
            ws.write(i, 1, value, f_cell)

    w = wb.add_worksheet('Marcaciones')
    headers = ['Fecha', 'Hora', 'Persona', 'Nº', 'Empresa', 'Método', 'Estado', 'Visita', 'Detalle']
    for c, h in enumerate(headers):
        w.write(0, c, h, f_h)
    for c, width in enumerate([12, 10, 34, 14, 26, 16, 20, 8, 60]):
        w.set_column(c, c, width)
    for r, e in enumerate(events, start=1):
        row = [_local_str(e.event_time, '%d-%m-%Y'), _local_str(e.event_time, '%H:%M:%S'),
               e.person_name or e.employee_no, e.employee_no, e.company, e.verify_method,
               e.get_status_display(), 'sí' if e.is_visitor else '', e.detail or '']
        for c, value in enumerate(row):
            w.write(r, c, value, f_cell)

    wb.close()
    return buf.getvalue()
