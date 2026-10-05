"""
Programador de envíos del informe por correo.

    python manage.py send_scheduled_reports            # un ciclo y termina (cron, pruebas)
    python manage.py send_scheduled_reports --loop     # servicio: un ciclo cada 30 s

En docker-compose corre como el servicio `scheduler`, con la misma imagen que la web.
"""
import logging
import signal
import threading

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from backend.apps.reports.mailing import run_scheduler_tick

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Envía por correo los informes programados a los que les llegó la hora.'

    def add_arguments(self, parser):
        parser.add_argument('--loop', action='store_true',
                            help='Quedarse corriendo y revisar cada --interval segundos.')
        parser.add_argument('--interval', type=int, default=30,
                            help='Segundos entre ciclos con --loop (por defecto 30).')

    def handle(self, *args, **options):
        if not options['loop']:
            sent = run_scheduler_tick()
            self.stdout.write(f'Envíos realizados: {sent}')
            return

        stop = threading.Event()

        def _stop(signum, _frame):
            # docker stop manda SIGTERM: se termina el envío en curso y se sale
            logger.info('Programador de envíos: señal %s, deteniendo…', signum)
            stop.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        interval = max(5, options['interval'])
        logger.info('Programador de envíos iniciado (cada %s s).', interval)
        while not stop.is_set():
            # la conexión a la base puede haberse caído entre ciclos (reinicio de Postgres)
            close_old_connections()
            try:
                run_scheduler_tick()
            except Exception:
                # nunca botar el servicio por un ciclo con error: se reintenta en el siguiente
                logger.exception('Error en el ciclo del programador de envíos')
            stop.wait(interval)
        logger.info('Programador de envíos detenido.')
