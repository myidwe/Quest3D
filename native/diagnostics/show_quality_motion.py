"""Visible, self-closing PC test window for the existing real capture pipeline.

The window is source content only: it does not synthesize depth, capture pixels,
inject input, or alter the producer. Escape and its Close button end it early.
"""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import time
import tkinter as tk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=45, choices=range(1, 61))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.relative_to(root / "artifacts")
    output.mkdir(parents=True, exist_ok=False)
    report = {"started_at": datetime.now().astimezone().isoformat(), "source_window_only": True,
              "synthetic_depth": False, "requests": [], "requested_seconds": args.seconds}
    window = tk.Tk()
    window.title("Quest3D PC video quality check - closes automatically")
    window.geometry("1000x580+120+120")
    window.attributes("-topmost", True)
    canvas = tk.Canvas(window, background="#303030", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_text(28, 28, anchor="nw", text="PC VIDEO: small text / moving edges", fill="white", font=("Segoe UI", 24))
    for i, size in enumerate((12, 14, 16, 20, 28)):
        canvas.create_text(28, 88 + i * 49, anchor="nw", fill="#eeeeee",
                           text=f"{size}px  Windows desktop 0123456789 AaBb  브라우저 화면 선명도",
                           font=("Malgun Gothic", -size))
    canvas.create_text(28, 365, anchor="nw", fill="#dddddd", font=("Segoe UI", 14),
                       text="Look for readable strokes, bright outlines and shimmer. Escape closes this window.")
    box = canvas.create_rectangle(50, 427, 115, 492, fill="#dadada", outline="white", width=2)
    timer = canvas.create_text(28, 520, anchor="nw", fill="white", font=("Segoe UI", 14))
    start = time.perf_counter()
    closed = False

    def close(reason="close_button"):
        nonlocal closed
        if closed:
            return
        closed = True
        report.update(finished_at=datetime.now().astimezone().isoformat(),
                      elapsed_seconds=time.perf_counter() - start, end_reason=reason)
        window.destroy()

    def tick():
        elapsed = time.perf_counter() - start
        if elapsed >= args.seconds:
            close("automatic")
            return
        x = 420 + 350 * math.sin(elapsed * 1.2)
        canvas.coords(box, x, 427, x + 65, 492)
        canvas.itemconfigure(timer, text=f"Auto close: {math.ceil(args.seconds - elapsed)}s")
        report["requests"].append({"elapsed": elapsed, "rectangle_x": x})
        window.after(25, tick)

    window.protocol("WM_DELETE_WINDOW", close)
    window.bind("<Escape>", lambda event: close("escape"))
    tick()
    try:
        window.mainloop()
    finally:
        (output / "window.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "requests"}))


if __name__ == "__main__":
    main()
