"""Conservative face gate sampled only while narration is audible."""

from __future__ import annotations

import argparse
import json
import math
import wave
from array import array
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
PREFLIGHT_VERSION = "haar-active-speech-v1"


def _active_times(path: Path, *, sample_count: int) -> list[float]:
    with wave.open(str(path), "rb") as stream:
        sample_rate = stream.getframerate()
        channels = stream.getnchannels()
        sample_width = stream.getsampwidth()
        if sample_rate <= 0 or channels <= 0 or sample_width != 2:
            raise ValueError("Narration WAV must be 16-bit PCM")
        frames_per_window = max(1, sample_rate // 10)
        active: list[float] = []
        frame_offset = 0
        while True:
            payload = stream.readframes(frames_per_window)
            if not payload:
                break
            samples = array("h")
            samples.frombytes(payload)
            if samples:
                rms = math.sqrt(sum(value * value for value in samples) / len(samples))
                if rms >= 200:
                    active.append((frame_offset + frames_per_window / 2) / sample_rate)
            frame_offset += len(samples) // channels
    if len(active) <= sample_count:
        return active
    return [
        active[round(index * (len(active) - 1) / (sample_count - 1))]
        for index in range(sample_count)
    ]


def inspect_video(
    video_path: Path,
    audio_path: Path,
    *,
    sample_count: int = 16,
    minimum_face_ratio: float = 0.015,
) -> dict[str, Any]:
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("OpenCV is required by the lip-sync backend environment") from error

    times = _active_times(audio_path, sample_count=sample_count)
    if not times:
        return {
            "schema_version": SCHEMA_VERSION,
            "preflight_version": PREFLIGHT_VERSION,
            "decision": "no_speech",
            "sample_count": 0,
            "safe_samples": 0,
            "no_face_samples": 0,
            "ambiguous_samples": 0,
            "small_face_samples": 0,
        }
    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(str(cascade_path))
    if detector.empty():
        raise RuntimeError("OpenCV frontal-face cascade is unavailable")
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError("Cannot open dubbed video for face preflight")

    safe = no_face = ambiguous = small = 0
    try:
        for seconds in times:
            capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000.0)
            ok, frame = capture.read()
            if not ok or frame is None:
                no_face += 1
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = detector.detectMultiScale(
                gray,
                scaleFactor=1.1,
                minNeighbors=5,
                minSize=(32, 32),
            )
            if len(faces) == 0:
                no_face += 1
                continue
            if len(faces) > 1:
                ambiguous += 1
                continue
            _x, _y, width, height = faces[0]
            frame_area = max(1, int(frame.shape[0]) * int(frame.shape[1]))
            if int(width) * int(height) / frame_area < minimum_face_ratio:
                small += 1
                continue
            safe += 1
    finally:
        capture.release()

    decision = "safe"
    if ambiguous:
        decision = "ambiguous_faces"
    elif no_face:
        decision = "no_face"
    elif small:
        decision = "face_too_small"
    return {
        "schema_version": SCHEMA_VERSION,
        "preflight_version": PREFLIGHT_VERSION,
        "decision": decision,
        "sample_count": len(times),
        "safe_samples": safe,
        "no_face_samples": no_face,
        "ambiguous_samples": ambiguous,
        "small_face_samples": small,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--sample-count", type=int, default=16)
    parser.add_argument("--minimum-face-ratio", type=float, default=0.015)
    args = parser.parse_args()
    try:
        report = inspect_video(
            args.video,
            args.audio,
            sample_count=args.sample_count,
            minimum_face_ratio=args.minimum_face_ratio,
        )
    except Exception as error:
        print(json.dumps({"error": type(error).__name__}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
