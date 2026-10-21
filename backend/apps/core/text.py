"""Normalización de texto para buscar personas: sin tildes, sin mayúsculas, solo dígitos."""
import unicodedata


def fold(text):
    """Minúsculas y sin tildes, para buscar «munoz» y encontrar «Muñoz»."""
    text = unicodedata.normalize('NFD', (text or '').lower())
    return ''.join(ch for ch in text if unicodedata.category(ch) != 'Mn')


def digits_of(text):
    """Solo dígitos (y una K final de RUT): «12.345.678-9» → «123456789»."""
    return ''.join(ch for ch in (text or '').upper() if ch.isdigit() or ch == 'K')
