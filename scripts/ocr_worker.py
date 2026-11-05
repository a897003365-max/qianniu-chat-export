#!/usr/bin/env python3
"""Read image paths as JSON lines, return OCR JSON; never write image/output files."""
import contextlib
import json
import sys


def main():
    with contextlib.redirect_stdout(sys.stderr):
        import cv2
        import numpy as np
        from PIL import Image, ImageSequence
        from rapidocr_onnxruntime import RapidOCR
        from importlib.metadata import version
        cv2.setNumThreads(1)
        engine = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
        engine_version = version("rapidocr_onnxruntime")
    for line in sys.stdin:
        try:
            request = json.loads(line)
            frames = []
            with contextlib.redirect_stdout(sys.stderr), Image.open(request["path"]) as image:
                for index, frame in enumerate(ImageSequence.Iterator(image)):
                    pixels = cv2.cvtColor(np.asarray(frame.convert("RGB")), cv2.COLOR_RGB2BGR)
                    result, elapsed = engine(pixels)
                    entries = [{"box": [[float(v) for v in point] for point in row[0]],
                                "text": str(row[1]), "confidence": float(row[2])} for row in (result or [])]
                    frames.append({"frame_index": index, "width": frame.width, "height": frame.height,
                                   "entries": entries, "text": "\n".join(entry["text"] for entry in entries),
                                   "elapsed_seconds": [float(value) for value in elapsed] if elapsed is not None else None})
            response = {"ok": True, "result": {"engine": "rapidocr_onnxruntime", "version": engine_version, "frames": frames}}
        except Exception as error:
            response = {"ok": False, "error_type": type(error).__name__}
        print(json.dumps(response, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
