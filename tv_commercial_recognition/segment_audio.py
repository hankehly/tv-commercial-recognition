import argparse
import logging
from pathlib import Path
from typing import Any

import ffmpeg
from pydantic import BaseModel


class AudioSegmenter(BaseModel):
    input_file: str
    output_path: str
    min_segment_duration: float = 10
    max_segment_duration: float = 60
    silence_noise: int = -100
    silence_duration: float = 0.8
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
        out, _ = (
            ffmpeg.input(self.input_file)
            .filter(
                "silencedetect",
                noise=f"{self.silence_noise}dB",
                d=self.silence_duration,
            )
            .filter("ametadata", mode="print", file="-")
            .output("-", format="null")
            .overwrite_output()
            .run(quiet=True)
        )

        Path(self.output_path).mkdir(parents=True, exist_ok=self.overwrite)
        input_file_extension = Path(self.input_file).suffix

        start_time = 0
        segment_counter = 0
        for line in out.split(b"\n"):
            if b"silence_end" in line:
                silence_end = float(line.split(b"=")[1])
                segment_duration = silence_end - start_time
                bound_lower_ok = segment_duration >= self.min_segment_duration
                bound_upper_ok = segment_duration <= self.max_segment_duration
                if bound_lower_ok and bound_upper_ok:
                    output_file = f"{self.output_path}/segment_{segment_counter:05d}{input_file_extension}"
                    output_stream = ffmpeg.input(
                        self.input_file, ss=start_time, to=silence_end
                    )
                    output_stream.output(
                        output_file, acodec="copy"
                    ).overwrite_output().run(quiet=True)
                    self.log.info(
                        "Saved segment %d (%.2f seconds) to %s",
                        segment_counter,
                        segment_duration,
                        output_file,
                    )
                else:
                    self.log.warning(
                        "Segment %d is too short or too long (%.2f seconds), skipping..",
                        segment_counter,
                        segment_duration,
                    )
                segment_counter += 1
                start_time = silence_end


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Segment audio based on silence detection."
    )
    parser.add_argument("input_file", help="Input audio file")
    parser.add_argument("output_path", help="Output directory or path for segments")
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
        "--silence-noise",
        type=int,
        default=-100,
        help="Silencedetect noise level in dB (default: -100)",
    )
    parser.add_argument(
        "--silence-duration",
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
        input_file=args.input_file,
        output_path=args.output_path,
        min_segment=args.min_segment,
        max_segment=args.max_segment,
        silence_noise=args.silence_noise,
        silence_duration=args.silence_duration,
        overwrite=args.overwrite,
    )
    segmenter.configure_logging(args.log_level)
    segmenter.execute()
