import argparse
import signal
import datetime
import logging
import re
from pathlib import Path
from subprocess import PIPE, Popen
from tempfile import NamedTemporaryFile
from typing import Any

from pydantic import BaseModel
from pydub import AudioSegment
from pydub.silence import detect_leading_silence


class AudioSegmenter(BaseModel):
    """
    A class for segmenting audio files based on silence detection.
    Requires ffmpeg to be installed.
    """

    input_audio_device: str
    output_path: str
    min_segment_duration: float = 10
    max_segment_duration: float = 60
    detect_silence_noise: int = -100
    detect_silence_duration: float = 0.8
    overwrite: bool = False
    _log: logging.Logger = None
    _shutdown: bool = False

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

    @_check_shutdown
    def execute(self):
        Path(self.output_path).mkdir(parents=True, exist_ok=self.overwrite)
        # Handle SIGTERM gracefully
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        # If the input audio is silent from the beginning, the first segment will be something like 0.05 seconds long,
        # which is too short and will be filtered out later.
        next_segment_start = 0.0
        with NamedTemporaryFile(suffix=".wav", dir=self.output_path) as tmp:
            tmp_audio_path = str(Path(self.output_path) / tmp.name)
            # fmt: off
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-f", "avfoundation", "-i", f":{self.input_audio_device}",
                "-af", f"silencedetect=noise={self.detect_silence_noise}dB:d={self.detect_silence_duration}",
                "-f", "wav", tmp_audio_path,
            ]
            # fmt: on
            pattern = r"silence_end: (\d+\.\d+) \| silence_duration: (\d+\.\d+)"
            with Popen(ffmpeg_cmd, stderr=PIPE) as p:
                while not self._shutdown:
                    try:
                        line = p.stderr.read1().decode("utf-8")
                        match = re.search(pattern, line)
                        if match:
                            silence_end = float(match.group(1))
                            silence_duration = float(match.group(2))
                            self._export_segment(
                                next_segment_start,
                                tmp_audio_path,
                                silence_end,
                                silence_duration,
                            )
                            next_segment_start = silence_end
                    except KeyboardInterrupt:
                        self.log.info("Received KeyboardInterrupt, shutting down...")
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
        if self.min_segment_duration <= segment_duration <= self.max_segment_duration:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            export_path = f"{self.output_path}/segment_{timestamp}_{round(segment_duration, 1)}s.wav"
            segment_start_ms = segment_start * 1000
            segment_end_ms = (silence_end - silence_duration) * 1000
            segment = AudioSegment.from_wav(audio_file_path)
            segment = segment[segment_start_ms:segment_end_ms]
            segment_end_silence_ms = detect_leading_silence(
                segment.reverse(), silence_threshold=self.detect_silence_noise
            )
            segment[:-segment_end_silence_ms].export(export_path, format="wav")
            self.log.info(
                "Saved segment (%.2f seconds) to %s", segment_duration, export_path
            )
        else:
            self.log.info(
                "Segment is too short or too long (%.2f seconds), skipping..",
                segment_duration,
            )


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
        default=0.8,
        help="Silencedetect duration threshold in seconds (default: 0.8)",
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

    args = parser.parse_args()
    segmenter = AudioSegmenter(
        input_audio_device=args.input_audio_device,
        output_path=args.output_path,
        detect_silence_noise=args.detect_silence_noise,
        detect_silence_duration=args.detect_silence_duration,
        min_segment_duration=args.min_segment,
        max_segment_duration=args.max_segment,
        overwrite=args.overwrite,
    )
    segmenter.configure_logging(args.log_level)
    segmenter.execute()
