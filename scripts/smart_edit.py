"""Smart-speed editor for agent screen recordings: real action at 1x, waiting fast-forwarded.

  python scripts/smart_edit.py out.mp4 "clip1.mp4@0-126|1) Message to myself" "clip2.mp4|2) Message to Tharun"

Each input: path[@start-end][|caption]. Motion is measured at 4 fps; quiet stretches (model thinking)
are sped up (badge shows the speed), active stretches play at normal speed.
"""
import subprocess
import sys
from pathlib import Path

import numpy as np

FPS, W, H = 4, 320, 180
FONT = "C\\:/Windows/Fonts/segoeuib.ttf"


def motion(path, start, end):
    cmd = ["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(end - start), "-i", path,
           "-vf", f"fps={FPS},scale={W}:{H},format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(np.int16)
    diff = (np.abs(np.diff(frames, axis=0)) > 18).mean(axis=(1, 2))      # fraction of changed pixels
    return np.concatenate([[diff[0] if len(diff) else 0], diff])


def segments(score, active_thr=0.0015, pad=2):
    act = score > active_thr
    act = np.convolve(act, np.ones(2 * pad + 1), "same") > 0             # keep a little context around action
    segs, i = [], 0
    while i < len(act):
        j = i
        while j < len(act) and act[j] == act[i]:
            j += 1
        segs.append([i / FPS, j / FPS, bool(act[i])])
        i = j
    out = []
    for s, e, a in segs:                                                 # idle < 1.5 s stays at normal speed
        a = a or (e - s) < 1.5
        if out and out[-1][2] == a:
            out[-1][1] = e
        else:
            out.append([s, e, a])
    return out


def esc(t):
    return t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace(",", "\\,")


def main(out, specs):
    chains, labels, n, total = [], [], 0, 0.0
    for k, spec in enumerate(specs):
        spec, _, caption = spec.partition("|")
        path, _, rng = spec.partition("@")
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    path], capture_output=True, text=True).stdout)
        start, end = (map(float, rng.split("-")) if rng else (0.0, dur))
        segs = segments(motion(path, start, end))
        print(f"{Path(path).name}: {end - start:.0f}s -> {len(segs)} segments")
        for si, (s, e, active) in enumerate(segs):
            speed = 1.0 if active else min(30.0, max(4.0, (e - s) / 1.0))      # waiting shrinks to ~1 s
            f = (f"[{k}:v]trim={start + s:.3f}:{start + e:.3f},setpts=(PTS-STARTPTS)/{speed:.3f},"
                 f"fps=30,scale=trunc(iw/2)*2:trunc(ih/2)*2")
            if not active:
                f += (f",drawtext=fontfile='{FONT}':text='{esc(f'>> {speed:.0f}x')}':x=w-tw-40:y=40:fontsize=44:"
                      f"fontcolor=white:box=1:boxcolor=0x8B5CF6@0.85:boxborderw=14")
            if si == 0 and caption:
                f += (f",drawtext=fontfile='{FONT}':text='{esc(caption)}':x=(w-tw)/2:y=h-150:fontsize=52:"
                      f"fontcolor=white:box=1:boxcolor=0x000000@0.6:boxborderw=20:enable='lt(t,3.5)'")
            chains.append(f + f"[v{n}]")
            labels.append(f"[v{n}]")
            total += (e - s) / speed
            n += 1
            print(f"   {s:6.1f}-{e:6.1f}s {'ACTION' if active else 'wait  '} x{speed:4.1f}")
    graph = ";\n".join(chains) + ";\n" + "".join(labels) + f"concat=n={n}:v=1:a=0[out]"
    script = Path(out).with_suffix(".filter.txt")
    script.write_text(graph, encoding="utf-8")
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for spec in specs:
        cmd += ["-i", spec.partition("|")[0].partition("@")[0]]
    cmd += ["-/filter_complex", str(script), "-map", "[out]", "-c:v", "libx264", "-preset", "medium",
            "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True)
    script.unlink()
    print(f"saved {out}  (~{total:.0f}s)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
