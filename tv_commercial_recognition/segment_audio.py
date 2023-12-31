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

from tv_commercial_recognition.tasks import export_segment


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
        logger = logging.getLogger(
            f"{self.__class__.__module__}.{self.__class__.__name__}"
        )
        formatter = logging.Formatter(
            '{"asctime": "%(asctime)s", "name": "%(name)s", "levelname": "%(levelname)s", "message": %(message)s}'
        )
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self._log = logger

    def _check_shutdown(func):
        def wrapper(self, *args, **kwargs):
            if self._shutdown:
                self.log.info(
                    json.dumps(
                        {
                            "message": "Shutdown in progress, skipping",
                        }
                    )
                )
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
            self.log.info(
                json.dumps(
                    {
                        "message": "Overwriting existing directory",
                        "segments_path": str(self.segments_path),
                    }
                )
            )

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

            self.log.info(
                json.dumps(
                    {
                        "message": "Starting ffmpeg process",
                        "ffmpeg_cmd": ffmpeg_cmd,
                    }
                )
            )
            with Popen(ffmpeg_cmd, stderr=PIPE) as p:
                while not self._shutdown:
                    try:
                        line = p.stderr.read1().decode("utf-8")
                        match = re.search(silence_pattern, line)
                        if match:
                            silence_end_seconds = float(match.group(1))
                            silence_duration_seconds = float(match.group(2))
                            self.log.info(
                                json.dumps(
                                    {
                                        "message": "Silence detected",
                                        "silence_end_seconds": silence_end_seconds,
                                        "silence_duration_seconds": silence_duration_seconds,
                                    }
                                )
                            )
                            segment_duration = (
                                silence_end_seconds
                                - silence_duration_seconds
                                - next_segment_start
                            )
                            if (
                                self.min_segment_duration
                                <= segment_duration
                                <= self.max_segment_duration
                            ):
                                self.log.info(
                                    json.dumps(
                                        {
                                            "message": "Queuing segment export",
                                            "audio_file_path": tmp_audio_path,
                                            "segment_start": next_segment_start,
                                            "silence_end_seconds": silence_end_seconds,
                                            "silence_duration_seconds": silence_duration_seconds,
                                            "segments_path": str(self.segments_path),
                                            "detect_silence_noise": self.detect_silence_noise,
                                        }
                                    )
                                )
                                segment_end_seconds = (
                                    silence_end_seconds - silence_duration_seconds
                                )
                                export_segment.delay(
                                    tmp_audio_path,
                                    next_segment_start,
                                    segment_end_seconds,
                                    str(self.segments_path),
                                    self.detect_silence_noise,
                                )
                            else:
                                self.log.info(
                                    json.dumps(
                                        {
                                            "message": "Segment is too short or too long",
                                            "segment_duration": segment_duration,
                                        }
                                    )
                                )
                            next_segment_start = silence_end_seconds
                            file_size = os.path.getsize(tmp_audio_path)
                            file_too_big = file_size > self.max_temp_file_size_bytes
                            if file_too_big:
                                self.log.info(
                                    json.dumps(
                                        {
                                            "message": "Temporary file is too big",
                                            "file_size": file_size,
                                            "max_temp_file_size_bytes": self.max_temp_file_size_bytes,
                                        }
                                    )
                                )
                                p.terminate()  # necessary to prevent hanging
                                break  # restart ffmpeg process
                            else:
                                self.log.info(
                                    f"Temporary file size is {file_size} bytes"
                                )
                    except KeyboardInterrupt:
                        self.log.info(
                            json.dumps(
                                {
                                    "message": "KeyboardInterrupt",
                                }
                            )
                        )

                        p.terminate()
                        self._shutdown = True

    def _handle_sigterm(self, *args):
        self.log.info(
            json.dumps(
                {
                    "message": "Received SIGTERM",
                }
            )
        )
        self._shutdown = True


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
        max_temp_file_size_bytes=args.max_temp_file_size_bytes,
    )
    segmenter.configure_logging(args.log_level)
    segmenter.execute()
