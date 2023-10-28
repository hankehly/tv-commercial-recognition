import argparse
import datetime
import logging
import re
import tempfile
from pathlib import Path
from subprocess import PIPE, Popen
from typing import Any

import pydub
from pydantic import BaseModel


class AudioSegmenter(BaseModel):
    input_audio_device: str
    output_path: str
    min_segment_duration: float = 10
    max_segment_duration: float = 60
    detect_silence_noise: int = -100
    detect_silence_duration: float = 0.8
    overwrite: bool = False
    _log: logging.Logger = None

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

    def execute(self):
        Path(self.output_path).mkdir(parents=True, exist_ok=self.overwrite)

        # If the input audio is silent from the beginning, then the first segment will be something like 0.05 seconds long,
        # which is too short and will be skipped automatically.
        start_time = 0.0

        with tempfile.NamedTemporaryFile(suffix=".wav", dir=self.output_path) as file:
            audio_file = str(Path(self.output_path) / file.name)
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "avfoundation",
                "-i",
                f":{self.input_audio_device}",
                "-af",
                f"silencedetect=noise={self.detect_silence_noise}dB:d={self.detect_silence_duration}",
                "-f",
                "wav",
                audio_file,
            ]
            with Popen(cmd, stderr=PIPE) as p:
                while True:
                    try:
                        line = p.stderr.read1().decode("utf-8")
                        silence_end_match = re.search(
                            r"silence_end: (\d+\.\d+) \| silence_duration: (\d+\.\d+)",
                            line,
                        )
                        if silence_end_match:
                            silence_end = float(silence_end_match.group(1))
                            silence_duration = float(silence_end_match.group(2))
                            segment_duration = (
                                silence_end - silence_duration - start_time
                            )
                            if (
                                self.min_segment_duration
                                <= segment_duration
                                <= self.max_segment_duration
                            ):
                                ts = datetime.datetime.now().strftime(
                                    "%Y-%m-%d_%H-%M-%S"
                                )
                                output_file = f"{self.output_path}/segment_{ts}_{silence_duration}s.wav"
                                segment = pydub.AudioSegment.from_wav(audio_file)[
                                    start_time
                                    * 1000 : (silence_end - silence_duration)
                                    * 1000
                                ]
                                extra_silence_length = (
                                    pydub.silence.detect_leading_silence(
                                        segment.reverse(), silence_threshold=-100
                                    )
                                )
                                segment[:-extra_silence_length].export(
                                    output_file, format="wav"
                                )
                                self.log.info(
                                    "Saved segment (%.2f seconds) to %s",
                                    segment_duration,
                                    output_file,
                                )
                            else:
                                self.log.info(
                                    "Segment is too short or too long (%.2f seconds), skipping..",
                                    segment_duration,
                                )
                            start_time = silence_end
                    except KeyboardInterrupt:
                        break


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
