"""
Feriados legales de Chile, como lista de referencia para cargar de una vez en «Feriados».

Son los feriados nacionales (irrenunciables y comunes) con los traslados que fija la ley
para San Pedro y San Pablo, el Encuentro de Dos Mundos y las Iglesias Evangélicas. No
incluye feriados regionales ni los que se decretan para elecciones. La lista se revisa en
pantalla y se puede corregir a mano: solo se agregan los días que faltan.
"""
from datetime import date

CHILE_HOLIDAYS = {
    2026: [
        (date(2026, 1, 1), 'Año Nuevo'),
        (date(2026, 4, 3), 'Viernes Santo'),
        (date(2026, 4, 4), 'Sábado Santo'),
        (date(2026, 5, 1), 'Día Nacional del Trabajo'),
        (date(2026, 5, 21), 'Día de las Glorias Navales'),
        (date(2026, 6, 21), 'Día Nacional de los Pueblos Indígenas'),
        (date(2026, 6, 29), 'San Pedro y San Pablo'),
        (date(2026, 7, 16), 'Día de la Virgen del Carmen'),
        (date(2026, 8, 15), 'Asunción de la Virgen'),
        (date(2026, 9, 18), 'Independencia Nacional'),
        (date(2026, 9, 19), 'Día de las Glorias del Ejército'),
        (date(2026, 10, 12), 'Encuentro de Dos Mundos'),
        (date(2026, 10, 31), 'Día de las Iglesias Evangélicas y Protestantes'),
        (date(2026, 11, 1), 'Día de Todos los Santos'),
        (date(2026, 12, 8), 'Inmaculada Concepción'),
        (date(2026, 12, 25), 'Navidad'),
    ],
    2027: [
        (date(2027, 1, 1), 'Año Nuevo'),
        (date(2027, 3, 26), 'Viernes Santo'),
        (date(2027, 3, 27), 'Sábado Santo'),
        (date(2027, 5, 1), 'Día Nacional del Trabajo'),
        (date(2027, 5, 21), 'Día de las Glorias Navales'),
        (date(2027, 6, 21), 'Día Nacional de los Pueblos Indígenas'),
        (date(2027, 6, 28), 'San Pedro y San Pablo'),
        (date(2027, 7, 16), 'Día de la Virgen del Carmen'),
        (date(2027, 8, 15), 'Asunción de la Virgen'),
        (date(2027, 9, 18), 'Independencia Nacional'),
        (date(2027, 9, 19), 'Día de las Glorias del Ejército'),
        (date(2027, 10, 11), 'Encuentro de Dos Mundos'),
        (date(2027, 10, 31), 'Día de las Iglesias Evangélicas y Protestantes'),
        (date(2027, 11, 1), 'Día de Todos los Santos'),
        (date(2027, 12, 8), 'Inmaculada Concepción'),
        (date(2027, 12, 25), 'Navidad'),
    ],
}


def available_years():
    return sorted(CHILE_HOLIDAYS)


def load_year(year, created_by_name=''):
    """Agrega a `Holiday` los feriados del año que falten. Devuelve cuántos agregó."""
    from backend.apps.core.models import Holiday
    added = 0
    for day, name in CHILE_HOLIDAYS.get(year, []):
        _, created = Holiday.objects.get_or_create(
            date=day, defaults={'name': name, 'created_by_name': created_by_name})
        added += int(created)
    return added
