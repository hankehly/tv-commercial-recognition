import logging

from celery import Celery
from dejavu import Dejavu

# OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES celery -A tv_commercial_recognition.tasks worker --loglevel=INFO
app = Celery("tasks", broker="pyamqp://guest@localhost//")
app.conf.update(
    task_serializer="json",
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)


logger = logging.getLogger(__name__)


@app.task
def fingerprint_audio(file_path):
    logger.info(f"Fingerprinting audio file: {file_path}")
    djv = Dejavu(
        {
            "database_type": "postgres",
            "database": {
                "host": "localhost",
                "user": "postgres",
                "password": "postgres",
                "database": "tv-commercial-recognition",
            },
        }
    )
    djv.fingerprint_file(file_path)
    logger.info(f"Finished fingerprinting audio file: {file_path}")
