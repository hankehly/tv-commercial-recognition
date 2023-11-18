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
    input_audio_file_path: str,
    segment_start_seconds: float,
    segment_end_seconds: float,
    output_directory_path: str,
    detect_silence_noise: float,
):
    # todo: this condition checks the segment duration while it still has the silence
    # at the end so this may not be the actual duration of the segment
    current_timestamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
    save_path = str(Path(output_directory_path) / f"segment_{current_timestamp}.mp3")
    segment = AudioSegment.from_mp3(input_audio_file_path)
    audio_file_len_seconds = segment.duration_seconds

    # segment_end_seconds represents the right boundary of the clip
    # audio_file_len_seconds represents the right boundary of the audio file
    # if segment_end_seconds extends beyond the audio_file_len_seconds, then we need to reschedule the task
    # do so based on the difference between segment_end_seconds and audio_file_len_seconds
    if segment_end_seconds > audio_file_len_seconds:
        logger.info(
            json.dumps(
                {
                    "message": "Rescheduling task",
                    "start_sec": segment_start_seconds,
                    "segment_end_seconds": segment_end_seconds,
                    "audio_file_len_seconds": audio_file_len_seconds,
                    "segment_duration": segment.duration_seconds,
                    "save_path": save_path,
                }
            )
        )
        # If segment_end_seconds surpasses audio_file_len_seconds by 3 seconds
        # then we should reschedule 15 seconds later
        multiplier = 5
        countdown = (segment_end_seconds - audio_file_len_seconds) * multiplier
        raise self.retry(countdown=countdown)

    start_ms = segment_start_seconds * 1000
    end_ms = segment_end_seconds * 1000
    segment = segment[start_ms:end_ms]
    logger.info(
        json.dumps(
            {
                "message": "Exporting segment",
                "input_audio_file_path": input_audio_file_path,
                "segment_start_seconds": segment_start_seconds,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start_sec": segment_start_seconds,
                "segment_end_seconds": segment_end_seconds,
                "audio_file_len_seconds": audio_file_len_seconds,
                "segment_duration": segment.duration_seconds,
                "save_path": save_path,
                "max_dBFS": segment.max_dBFS,
            }
        )
    )
    segment_end_silence_ms = detect_leading_silence(
        segment.reverse(), silence_threshold=detect_silence_noise
    )
    # If the silence is 0.0 seconds, slicing will result in empty segment
    # >>> [1, 2, 3][:-0]
    # []
    if segment_end_silence_ms > 0:
        segment = segment[:-segment_end_silence_ms]
    segment.export(save_path, format="mp3")
    logger.info(
        json.dumps(
            {
                "message": "Segment saved",
                "export_segment_duration": len(segment) / 1000,
                "save_path": save_path,
                "stripped_segment_end_silence_ms": segment_end_silence_ms,
            }
        )
    )


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
#   "input_audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_seconds_ms": 71.6182,
#   "silence_end_seconds": 83.6227,
#   "silence_duration_seconds": 1.10914,
#   "start_ms": 71618.2,
#   "end_ms": 82513.56,
#   "start_sec": 71.6182,
#   "segment_end_seconds": 82.51356,
#   "audio_file_len_seconds": 167.11943310657597, (85 second difference)
#   "segment_duration": 10.895351473922902,
#   "save_path": "output7/_segments/segment_20231118161013.mp3",
#   "max_dBFS": -6.504606544973271
# }
# {
#   "message": "Exporting segment",
#   "input_audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_seconds_ms": 176.508,
#   "silence_end_seconds": 191.519,
#   "silence_duration_seconds": 0.917891,
#   "start_ms": 176508.0,
#   "end_ms": 190601.109,
#   "start_sec": 176.508,
#   "segment_end_seconds": 190.601109,
#   "audio_file_len_seconds": 270.7210657596372, (80 second difference)
#   "segment_duration": 14.093106575963718,
#   "save_path": "output7/_segments/segment_20231118161201.mp3",
#   "max_dBFS": -6.047679457880003
# }
# {
#   "message": "Exporting segment",
#   "input_audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_seconds_ms": 206.542,
#   "silence_end_seconds": 221.561,
#   "silence_duration_seconds": 0.954603,
#   "start_ms": 206542.0,
#   "end_ms": 220606.39700000003,
#   "start_sec": 206.542,
#   "segment_end_seconds": 220.60639700000002,
#   "audio_file_len_seconds": 299.58637188208615, (79 second difference )
#   "segment_duration": 14.064399092970522,
#   "save_path": "output7/_segments/segment_20231118161231.mp3",
#   "max_dBFS": -7.115850474240269
# }
# {
#   "message": "Exporting segment",
#   "input_audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_seconds_ms": 666.519,
#   "silence_end_seconds": 696.54,
#   "silence_duration_seconds": 0.981338,
#   "start_ms": 666519.0,
#   "end_ms": 695558.6619999999,
#   "start_sec": 666.519,
#   "segment_end_seconds": 695.5586619999999,
#   "audio_file_len_seconds": 758.9235147392291, (63 second difference)
#   "segment_duration": 29.039659863945577,
#   "save_path": "output7/_segments/segment_20231118162026.mp3",
#   "max_dBFS": -6.318283075227561
# }
# {
#   "message": "Exporting segment",
#   "input_audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_seconds_ms": 756.525,
#   "silence_end_seconds": 771.529,
#   "silence_duration_seconds": 0.923039,
#   "start_ms": 756525.0,
#   "end_ms": 770605.961,
#   "start_sec": 756.525,
#   "segment_end_seconds": 770.605961,
#   "audio_file_len_seconds": 830.734126984127, (60 second difference)
#   "segment_duration": 14.08095238095238,
#   "save_path": "output7/_segments/segment_20231118162141.mp3",
#   "max_dBFS": -5.006979374755626
# }

# You can see here how the end_sec surpasses the audio_file_len_seconds.
# The negative difference will be cut off from the exported file.
# So we want end_sec to be less than audio_file_len_seconds by a margin of 3 seconds or so (for safety).
#
# {
#   "message": "Exporting segment",
#   "input_audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_seconds_ms": 1765.52,
#   "silence_end_seconds": 1780.51,
#   "silence_duration_seconds": 1.19175,
#   "start_ms": 1765520.0,
#   "end_ms": 1779318.25,
#   "start_sec": 1765.52,
#   "segment_end_seconds": 1779.31825,
#   "audio_file_len_seconds": 1778.4043310657596, (-1 second difference)
#   "segment_duration": 12.883990929705215,
#   "save_path": "output7/_segments/segment_20231118163830.mp3",
#   "max_dBFS": -7.824132520261383
# }
# {
#   "message": "Exporting segment",
#   "input_audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_seconds_ms": 1780.51,
#   "silence_end_seconds": 1810.53,
#   "silence_duration_seconds": 0.84356,
#   "start_ms": 1780510.0,
#   "end_ms": 1809686.44,
#   "start_sec": 1780.51,
#   "segment_end_seconds": 1809.68644,
#   "audio_file_len_seconds": 1804.7096371882087, (-4 second difference)
#   "segment_duration": 24.2,
#   "save_path": "output7/_segments/segment_20231118163900.mp3",
#   "max_dBFS": -7.174987647554014
# }
# {
#   "message": "Exporting segment",
#   "input_audio_file_path": "output7/_streams/temp_20231118160719.mp3",
#   "segment_start_seconds_ms": 2574.56,
#   "silence_end_seconds": 2589.54,
#   "silence_duration_seconds": 0.807823,
#   "start_ms": 2574560.0,
#   "end_ms": 2588732.1769999997,
#   "start_sec": 2574.56,
#   "segment_end_seconds": 2588.732177,
#   "audio_file_len_seconds": 2486.6361678004537,
#   "segment_duration": 0.0,
#   "save_path": "output7/_segments/segment_20231118165159.mp3",
#   "max_dBFS": -Infinity
# }
