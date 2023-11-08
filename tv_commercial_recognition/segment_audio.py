import argparse
import logging
import os
import re
import signal
import time
import json
from pathlib import Path
from subprocess import PIPE, Popen
from typing import Any

from pydantic import BaseModel
from pydub import AudioSegment
from pydub.silence import detect_leading_silence

from tv_commercial_recognition.tasks import fingerprint_audio


def null_hook(export_path):
    pass


class AudioSegmenter(BaseModel):
    """
    A class for segmenting audio files based on silence detection.
    Requires ffmpeg to be installed.

    The input to this class is an audio device, e.g. the laptop microphone or internal loopback device.
    If needed, it can be adjusted to read from a file instead.
    """

    input_audio_device: str
    output_path: str
    min_segment_duration: float = 10
    max_segment_duration: float = 60
    detect_silence_noise: int = -100
    detect_silence_duration: float = 0.75
    overwrite: bool = False
    after_export_hook: Any = null_hook
    max_temp_file_size_bytes: int = 128 * 1024 * 1024  # 128 MB by default

    _log: logging.Logger = None
    _shutdown: bool = False

    def model_post_init(self, *args) -> None:
        signal.signal(signal.SIGTERM, self._handle_sigterm)

    @property
    def log(self) -> logging.Logger:
        if self._log is None:
            self.configure_logging("info")
        return self._log

    def configure_logging(self, log_level: str):
        numeric_log_level = getattr(logging, log_level.upper(), None)
        if not isinstance(numeric_log_level, int):
            raise ValueError("Invalid log level: {}".format(log_level))
        logging.basicConfig(
            level=numeric_log_level,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        self._log = logging.getLogger(
            f"{self.__class__.__module__}.{self.__class__.__name__}"
        )

    def _check_shutdown(func):
        def wrapper(self, *args, **kwargs):
            if self._shutdown:
                self.log.info("Shutting down...")
                return
            return func(self, *args, **kwargs)

        return wrapper

    @property
    def segments_path(self) -> Path:
        return Path(self.output_path) / "_segments"

    @property
    def streams_path(self) -> Path:
        return Path(self.output_path) / "_streams"

    @_check_shutdown
    def execute(self):
        # Create output directory
        self.segments_path.mkdir(parents=True, exist_ok=self.overwrite)
        self.streams_path.mkdir(parents=True, exist_ok=self.overwrite)

        if self.overwrite:
            self.log.info("Overwriting existing output directory")

        silence_pattern = r"silence_end: (\d+\.\d+) \| silence_duration: (\d+\.\d+)"

        while not self._shutdown:
            # Define a unique temporary file name based on the current timestamp
            timestamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
            tmp_audio_path = str(self.streams_path / f"temp_{timestamp}.mp3")

            ffmpeg_cmd = [
                "ffmpeg",
                "-y",  # overwrite output file if it exists
                "-f",
                "avfoundation",
                "-i",
                f":{self.input_audio_device}",
                "-flush_packets",  # disable output buffering
                "1",
                "-ac",  # convert to mono
                "1",
                "-af",
                f"silencedetect=noise={self.detect_silence_noise}dB:d={self.detect_silence_duration}",
                "-vn",  # disable video recording
                "-f",
                "mp3",  # use mp3 instead of wav to reduce file size
                tmp_audio_path,
            ]

            # If the input audio is silent from the beginning, the first segment will be something like 0.05 seconds long,
            # which is too short and will be filtered out later.
            next_segment_start = 0.0

            self.log.info("Starting ffmpeg process...")
            with Popen(ffmpeg_cmd, stderr=PIPE) as p:
                while not self._shutdown:
                    try:
                        line = p.stderr.read1().decode("utf-8")
                        match = re.search(silence_pattern, line)
                        if match:
                            silence_end = float(match.group(1))
                            silence_duration = float(match.group(2))
                            self.log.debug(
                                "Found silence at %.2f seconds, duration %.2f seconds",
                                silence_end,
                                silence_duration,
                            )
                            # give ffmpeg a chance to write the audio file to disk
                            # otherwise segment file size may be 0 bytes
                            time.sleep(0.25)
                            self._export_segment(
                                next_segment_start,
                                tmp_audio_path,
                                silence_end,
                                silence_duration,
                            )
                            next_segment_start = silence_end
                            file_size = os.path.getsize(tmp_audio_path)
                            file_too_big = file_size > self.max_temp_file_size_bytes
                            if file_too_big:
                                self.log.info(
                                    f"Temporary file is too big ({file_size} bytes), restarting ffmpeg process..."
                                )
                                p.terminate()  # necessary to prevent hanging
                                break  # restart ffmpeg process
                            else:
                                self.log.info(
                                    f"Temporary file size is {file_size} bytes"
                                )
                    except KeyboardInterrupt:
                        self.log.info("Received KeyboardInterrupt, shutting down...")
                        p.terminate()
                        self._shutdown = True

    def _handle_sigterm(self, *args):
        self.log.info("Received SIGTERM, shutting down...")
        self._shutdown = True

    @_check_shutdown
    def _export_segment(
        self,
        segment_start: float,
        audio_file_path: str,
        silence_end: float,
        silence_duration: float,
    ):
        segment_duration = silence_end - silence_duration - segment_start
        # todo: this condition checks the segment duration while it still has the silence
        # at the end so this may not be the actual duration of the segment
        if self.min_segment_duration <= segment_duration <= self.max_segment_duration:
            timestamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
            export_path = str(self.segments_path / f"segment_{timestamp}.mp3")
            start_ms = segment_start * 1000
            end_ms = (silence_end - silence_duration) * 1000
            segment = AudioSegment.from_mp3(audio_file_path)[start_ms:end_ms]
            segment_end_silence_ms = detect_leading_silence(
                segment.reverse(), silence_threshold=self.detect_silence_noise
            )
            # If the silence is 0.0 seconds, the segment will be empty
            # >>> [1, 2, 3][:-0]
            # []
            if segment_end_silence_ms > 0:
                segment = segment[:-segment_end_silence_ms]
            segment.export(export_path, format="mp3")
            self.log.info(
                json.dumps(
                    {
                        "message": "Segment saved",
                        "segment_duration": len(segment) / 1000,
                        "export_path": export_path,
                        "segment_end_silence_ms": segment_end_silence_ms,
                    }
                )
            )
            self.after_export_hook(export_path)
        else:
            self.log.info(
                json.dumps(
                    {
                        "message": "Segment is too short or too long",
                        "segment_duration": segment_duration,
                    }
                )
            )


def after_export_hook(export_path):
    fingerprint_audio.delay(export_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Segment audio based on silence detection."
    )
    parser.add_argument(
        "input_audio_device",
        help='Input audio device, e.g. 2 (obtain with `ffmpeg -f avfoundation -list_devices true -i ""`)',
    )
    parser.add_argument("output_path", help="Output directory for segments")
    parser.add_argument(
        "--min-segment",
        type=float,
        default=10,
        help="Minimum segment duration in seconds (default: 10)",
    )
    parser.add_argument(
        "--max-segment",
        type=float,
        default=60,
        help="Maximum segment duration in seconds (default: 60)",
    )
    parser.add_argument(
        "--detect-silence-noise",
        type=int,
        default=-100,
        help="Silencedetect noise level in dB (default: -100)",
    )
    parser.add_argument(
        "--detect-silence-duration",
        type=float,
        default=0.75,
        help="Silencedetect duration threshold in seconds (default: 0.75)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow overwriting if the directory already exists (default: False)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error", "critical"],
        help="Set the log level (default: info)",
    )
    parser.add_argument(
        "--max-temp-file-size-bytes",
        type=int,
        default=128 * 1024 * 1024,
        help="Maximum temporary file size in bytes (default: 128 MB)",
    )

    args = parser.parse_args()
    segmenter = AudioSegmenter(
        input_audio_device=args.input_audio_device,
        output_path=args.output_path,
        detect_silence_noise=args.detect_silence_noise,
        detect_silence_duration=args.detect_silence_duration,
        min_segment_duration=args.min_segment,
        max_segment_duration=args.max_segment,
        overwrite=args.overwrite,
        # after_export_hook=after_export_hook,
        max_temp_file_size_bytes=args.max_temp_file_size_bytes,
    )
    segmenter.configure_logging(args.log_level)
    segmenter.execute()
