import logging

from celery import Celery
from dejavu import Dejavu
import logging
from pathlib import Path
import time
import json
from pydub import AudioSegment
from pydub.silence import detect_leading_silence
import sys

# OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES celery -A tv_commercial_recognition.tasks worker --loglevel=INFO
app = Celery("tasks", broker="pyamqp://guest@localhost//")
app.conf.update(
    task_serializer="json",
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)


logger = logging.getLogger(__name__)
formatter = logging.Formatter(
    '{"asctime": "%(asctime)s", "name": "%(name)s", "levelname": "%(levelname)s", "message": %(message)s}'
)
handler = logging.StreamHandler()
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)


# celery -A tv_commercial_recognition.tasks worker -l INFO
@app.task(bind=True)
def export_segment(
    self,
    segment_start: float,
    audio_file_path: str,
    silence_end: float,
    silence_duration: float,
    segments_path: str,
    detect_silence_noise: float,
):
    # todo: this condition checks the segment duration while it still has the silence
    # at the end so this may not be the actual duration of the segment
    timestamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
    export_path = str(Path(segments_path) / f"segment_{timestamp}.mp3")
    end_sec = silence_end - silence_duration
    segment = AudioSegment.from_mp3(audio_file_path)
    audio_file_len_seconds = segment.duration_seconds

    # end_sec represents the right boundary of the clip
    # audio_file_len_seconds represents the right boundary of the audio file
    # if end_sec extends beyond the audio_file_len_seconds, then we need to reschedule the task
    # do so based on the difference between end_sec and audio_file_len_seconds
    if end_sec > audio_file_len_seconds:
        logger.info(
            json.dumps(
                {
                    "message": "Rescheduling task",
                    "start_sec": segment_start,
                    "end_sec": end_sec,
                    "audio_file_len_seconds": audio_file_len_seconds,
                    "segment_duration": segment.duration_seconds,
                    "export_path": export_path,
                }
            )
        )
        # If end_sec surpasses audio_file_len_seconds by 3 seconds
        # then we should reschedule 15 seconds later
        multiplier = 5
        countdown = (end_sec - audio_file_len_seconds) * multiplier
        raise self.retry(countdown=countdown)

    start_ms = segment_start * 1000
    end_ms = end_sec * 1000
    segment = segment[start_ms:end_ms]
    logger.info(
        json.dumps(
            {
                "message": "Exporting segment",
                "audio_file_path": audio_file_path,
                "segment_start_ms": segment_start,
                "silence_end": silence_end,
                "silence_duration": silence_duration,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start_sec": segment_start,
                "end_sec": end_sec,
                "audio_file_len_seconds": audio_file_len_seconds,
                "segment_duration": segment.duration_seconds,
                "export_path": export_path,
                "max_dBFS": segment.max_dBFS,
            }
        )
    )
    segment_end_silence_ms = detect_leading_silence(
        segment.reverse(), silence_threshold=detect_silence_noise
    )
    # If the silence is 0.0 seconds, the segment will be empty
    # >>> [1, 2, 3][:-0]
    # []
    if segment_end_silence_ms > 0:
        segment = segment[:-segment_end_silence_ms]
    segment.export(export_path, format="mp3")
    logger.info(
        json.dumps(
            {
                "message": "Segment saved",
                "export_segment_duration": len(segment) / 1000,
                "export_path": export_path,
                "stripped_segment_end_silence_ms": segment_end_silence_ms,
            }
        )
    )
    # after_export_hook(export_path)


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


#
# As the clip gets longer, the commercial clip end time and the length of the entire audio file
# get closer and closer together. Once this reaches 0, the exported segment will be empty.
#
# {
#   "message": "Exporting segment",
#   "audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_ms": 71.6182,
#   "silence_end": 83.6227,
#   "silence_duration": 1.10914,
#   "start_ms": 71618.2,
#   "end_ms": 82513.56,
#   "start_sec": 71.6182,
#   "end_sec": 82.51356,
#   "audio_file_len_seconds": 167.11943310657597, (85 second difference)
#   "segment_duration": 10.895351473922902,
#   "export_path": "output7/_segments/segment_20231118161013.mp3",
#   "max_dBFS": -6.504606544973271
# }
# {
#   "message": "Exporting segment",
#   "audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_ms": 176.508,
#   "silence_end": 191.519,
#   "silence_duration": 0.917891,
#   "start_ms": 176508.0,
#   "end_ms": 190601.109,
#   "start_sec": 176.508,
#   "end_sec": 190.601109,
#   "audio_file_len_seconds": 270.7210657596372, (80 second difference)
#   "segment_duration": 14.093106575963718,
#   "export_path": "output7/_segments/segment_20231118161201.mp3",
#   "max_dBFS": -6.047679457880003
# }
# {
#   "message": "Exporting segment",
#   "audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_ms": 206.542,
#   "silence_end": 221.561,
#   "silence_duration": 0.954603,
#   "start_ms": 206542.0,
#   "end_ms": 220606.39700000003,
#   "start_sec": 206.542,
#   "end_sec": 220.60639700000002,
#   "audio_file_len_seconds": 299.58637188208615, (79 second difference )
#   "segment_duration": 14.064399092970522,
#   "export_path": "output7/_segments/segment_20231118161231.mp3",
#   "max_dBFS": -7.115850474240269
# }
# {
#   "message": "Exporting segment",
#   "audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_ms": 666.519,
#   "silence_end": 696.54,
#   "silence_duration": 0.981338,
#   "start_ms": 666519.0,
#   "end_ms": 695558.6619999999,
#   "start_sec": 666.519,
#   "end_sec": 695.5586619999999,
#   "audio_file_len_seconds": 758.9235147392291, (63 second difference)
#   "segment_duration": 29.039659863945577,
#   "export_path": "output7/_segments/segment_20231118162026.mp3",
#   "max_dBFS": -6.318283075227561
# }
# {
#   "message": "Exporting segment",
#   "audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_ms": 756.525,
#   "silence_end": 771.529,
#   "silence_duration": 0.923039,
#   "start_ms": 756525.0,
#   "end_ms": 770605.961,
#   "start_sec": 756.525,
#   "end_sec": 770.605961,
#   "audio_file_len_seconds": 830.734126984127, (60 second difference)
#   "segment_duration": 14.08095238095238,
#   "export_path": "output7/_segments/segment_20231118162141.mp3",
#   "max_dBFS": -5.006979374755626
# }

# You can see here how the end_sec surpasses the audio_file_len_seconds.
# The negative difference will be cut off from the exported file.
# So we want end_sec to be less than audio_file_len_seconds by a margin of 3 seconds or so (for safety).
#
# {
#   "message": "Exporting segment",
#   "audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_ms": 1765.52,
#   "silence_end": 1780.51,
#   "silence_duration": 1.19175,
#   "start_ms": 1765520.0,
#   "end_ms": 1779318.25,
#   "start_sec": 1765.52,
#   "end_sec": 1779.31825,
#   "audio_file_len_seconds": 1778.4043310657596, (-1 second difference)
#   "segment_duration": 12.883990929705215,
#   "export_path": "output7/_segments/segment_20231118163830.mp3",
#   "max_dBFS": -7.824132520261383
# }
# {
#   "message": "Exporting segment",
#   "audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_ms": 1780.51,
#   "silence_end": 1810.53,
#   "silence_duration": 0.84356,
#   "start_ms": 1780510.0,
#   "end_ms": 1809686.44,
#   "start_sec": 1780.51,
#   "end_sec": 1809.68644,
#   "audio_file_len_seconds": 1804.7096371882087, (-4 second difference)
#   "segment_duration": 24.2,
#   "export_path": "output7/_segments/segment_20231118163900.mp3",
#   "max_dBFS": -7.174987647554014
# }
# {
#   "message": "Exporting segment",
#   "audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_ms": 2574.56,
#   "silence_end": 2589.54,
#   "silence_duration": 0.807823,
#   "start_ms": 2574560.0,
#   "end_ms": 2588732.1769999997,
#   "start_sec": 2574.56,
#   "end_sec": 2588.732177,
#   "audio_file_len_seconds": 2486.6361678004537,
#   "segment_duration": 0.0,
#   "export_path": "output7/_segments/segment_20231118165159.mp3",
#   "max_dBFS": -Infinity
# }
