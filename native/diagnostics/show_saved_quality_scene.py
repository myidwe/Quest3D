"""Show the preserved original scene for real capture/AI/Quest inspection.

Source content only; this window never displays synthesized stereo or depth.
It closes automatically and does not change any producer or headset setting.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
import tkinter as tk

from PIL import Image, ImageTk


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seconds',type=int,default=30,choices=range(1,61))
    args=p.parse_args()
    root=Path(__file__).resolve().parents[2]
    source=args.source.resolve(strict=True);source.relative_to(root/'artifacts')
    output=args.output.resolve();output.relative_to(root/'artifacts')
    output.mkdir(parents=True,exist_ok=False)
    image=Image.open(source).convert('RGB')
    report=dict(started_at=datetime.now().astimezone().isoformat(),
        source=str(source),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        scope='Displays preserved original RGB only; real live capture/depth occurs in separate producer',
        requested_seconds=args.seconds)
    window=tk.Tk();window.title('Quest3D original scene comparison')
    # The diagnosed source monitor is the verified 2560x1440 desktop at (0,0).
    # Keep native source pixels; no screenshot/SBS/AI result is fed back.
    window.geometry(f'{image.width}x{image.height}+0+0')
    window.overrideredirect(True);window.attributes('-topmost',True)
    rendered=ImageTk.PhotoImage(image)
    label=tk.Label(window,image=rendered,borderwidth=0,highlightthickness=0)
    label.pack()
    start=time.perf_counter();closed=False
    def close(reason):
        nonlocal closed
        if closed:return
        closed=True;report.update(end_reason=reason,elapsed_seconds=time.perf_counter()-start,
            finished_at=datetime.now().astimezone().isoformat())
        window.destroy()
    window.bind('<Escape>',lambda event:close('escape'))
    window.protocol('WM_DELETE_WINDOW',lambda:close('window_close'))
    window.after(args.seconds*1000,lambda:close('automatic'))
    try:window.mainloop()
    finally:(output/'window.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
