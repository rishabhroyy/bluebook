"""Offline replay of a recorded session (record.py) through the real Tracker + Renderer on a fake clock, for tuning
on real data instead of guessing.

    uv run python -m bluebook_interactive.replay --record     # capture until Ctrl-C (spoken prompts), then replay
    uv run python -m bluebook_interactive.replay data/diag/run-<time>.jsonl [--target camera] [--motion-tau 0.25 ...]

The glasses are simulated: every publish is acked one recorded draw round trip later (cycling through the recorded
acks; the prior if there are none), so the Renderer paces itself exactly as it does live. Metrics, per face source:
  jitter_px   back-and-forth of the mark per display update (rms second difference / sqrt 6, ring or chevron) while
              the wearer is not turning fast (truth < MOVE_DEG_S)
  lag_ms      time shift that best aligns the mark with the truth while moving (0 = no lag); a chevron is compared
              with the band-edge point of the true direction
  err_px      rms distance mark <-> truth while moving, at zero shift
  flips_min   ring <-> chevron switches per tracked minute
  max_step_px largest mark movement in one display update
"Truth" = the raw head-frame direction to the target, centred-averaged over +-TRUTH_W_S (non-causal), at the moment
the drawing becomes visible (tick + round trip), projected with the same display model.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import math
import shutil
import statistics
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import numpy as np
from bluebook_core.config import config
from bluebook_core.protocol import ImageDrawn, StepChanged, StepTarget

from . import render
from .filters import HeadKalman
from .geom import edge_point, project
from .targets import PHONE
from .tracker import Tracker
from .wire import PoseHello, PoseMsg

SESSION = "replay"
DT = 0.005  # s, fake-clock step
TRUTH_W_S = 0.15  # centred average half-width of the truth
SPEED_W_S = 0.3  # truth speed: difference of two averages this far either side, each this wide
MOVE_DEG_S = 8.0  # truth faster than this = moving (lag is measured here)
SHIFTS_S = np.arange(-0.2, 0.61, 0.01)


@contextlib.contextmanager
def tuned(**kw) -> Iterator[None]:
    """Temporarily override render module constants (SWAY_DEG_S, TURN_DEG_S, K_SIGMA, SETTLE_TICKS, ...)."""
    old = {k: getattr(render, k) for k in kw}
    for k, v in kw.items():
        setattr(render, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(render, k, v)


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def replay(recs: list[dict], target: str = PHONE, head: dict | None = None, metrics=None, **consts) -> dict:
    """Run the records through Tracker + Renderer; return the metrics (overall and per face source).
    head: HeadKalman keyword overrides (motion_tau, vel_shrink, ...); consts: render module constants;
    metrics: replaces _metrics(rows, raw) (tests use it to see the per-tick rows)."""
    rtts = [r["d"]["at"] - r["d"]["t"] for r in recs if r["k"] == "ack" and r["d"].get("t") is not None]
    rtts = [x for x in rtts if 0 < x < 1.0] or [render.PRIOR_RTT_S]
    poses = [(r["now"], r) for r in recs if r["k"] in ("pose", "hello", "imu")]
    if not any(r["k"] == "pose" for _, r in poses):
        raise ValueError("no pose messages in the recording")
    ages = [r["now"] - r["d"]["t"] for _, r in poses if r["k"] == "pose"]
    synced = 0 <= statistics.median(ages) <= 0.5  # phone clock usable as capture time; else assume the prior age

    with tempfile.TemporaryDirectory() as tmp, tuned(**consts):
        clock = [poses[0][0]]
        tk = Tracker(Path(tmp) / "calib", lambda: clock[0])
        if head:
            tk.head = HeadKalman(**head)
        tk.targets.steps[SESSION] = StepChanged(
            session=SESSION, skill_id="replay", step_id="replay", targets=[StepTarget(object_id=target, name=target)]
        )
        r = tk.renderer(SESSION)
        r.rtt.extend(rtts[-r.rtt.maxlen :])
        raw: list[tuple[float, np.ndarray, np.ndarray, np.ndarray]] = []  # (capture time, R, t, target point)
        rows = []
        i, k, ack_at, pub_t = 0, 0, None, 0.0
        while i < len(poses) or ack_at is not None:
            now = clock[0] = clock[0] + DT
            while i < len(poses) and poses[i][0] <= now:
                rec = poses[i][1]
                i += 1
                if rec["k"] == "hello":
                    tk.hello = PoseHello.model_validate(rec["d"])
                    continue
                if rec["k"] == "imu":
                    tk.on_imu(SESSION, rec["d"]["x"], rec["d"]["y"], rec["d"]["z"])
                    continue
                m = PoseMsg.model_validate(rec["d"])
                tk.on_pose(m, rec["now"])
                if m.face and (X := _target_point(m, target, tk.world)) is not None:
                    ts = m.t if synced else rec["now"] - render.PRIOR_AGE_S
                    d = tk.mirror @ np.array(m.face.R).reshape(3, 3).T @ (X - np.array(m.face.t))
                    raw.append((ts, d / max(float(np.linalg.norm(d)), 1e-9)))
            if ack_at is not None and now >= ack_at:
                r.on_ack(ImageDrawn(session=SESSION, id=1, name="i", t=pub_t, ms=90, at=now), now)
                ack_at = None
            if not r.due(now) or tk.head.R is None:
                continue
            before = r.inflight
            r.tick(now)
            if r.inflight is not None and r.inflight != before:
                pub_t, ack_at = now, now + rtts[k % len(rtts)]
                k += 1
            if (mk := r.marks.get(target)) is None or mk.sm.x is None:
                continue
            rows.append(
                {
                    "t": now,
                    "x": mk.sm.x.copy(),
                    "chev": bool(mk.chevron),
                    "ok": tk.tracking(now),
                    "src": tk.face_src or tk.source,
                    "P": tk.display(SESSION).P(tk.source),
                    "rtt": statistics.median(r.rtt),
                    "lead": r.lead,
                }
            )
    return (metrics or _metrics)(rows, raw)


def _target_point(m: PoseMsg, target: str, world: bool) -> np.ndarray | None:
    if not world:
        return np.zeros(3) if target == PHONE else None
    return next((np.array(a.p) for a in m.anchors or [] if a.id == target), None)


def _truth(raw, ts: np.ndarray, at: float, w: float = TRUTH_W_S) -> np.ndarray | None:
    """Mean head-frame direction to the target over at +- w (unit), or None without data."""
    lo, hi = np.searchsorted(ts, [at - w, at + w])
    if hi - lo < 2:
        return None
    d = np.mean([x[1] for x in raw[lo:hi]], axis=0)
    return d / max(float(np.linalg.norm(d)), 1e-9)


def _goal(d: np.ndarray, P: np.ndarray, chev: bool) -> np.ndarray:
    """Where the mark should be for the true direction d: the ring position, or the chevron's band-edge point."""
    u, v, w = project(P, d)[0]
    return np.array(edge_point(u, v, w) if chev else (u, v))


def _metrics(rows: list[dict], raw) -> dict:
    raw.sort(key=lambda x: x[0])
    ts = np.array([x[0] for x in raw])
    out = {"all": _summ(rows, raw, ts)}
    for src in sorted({r["src"] for r in rows if r["ok"]}):
        out[src] = _summ([r for r in rows if r["src"] == src], raw, ts)
    return out


def _summ(rows: list[dict], raw, ts) -> dict:
    ok = [r for r in rows if r["ok"]]
    if len(ok) < 3:
        return {"ticks": len(ok)}
    for r in ok:
        at = r["t"] + r["rtt"]
        # wide windows: the rear camera's ~1-3 deg noise must not read as motion
        a, b = (_truth(raw, ts, at + s, SPEED_W_S) for s in (-SPEED_W_S, SPEED_W_S))
        ang = None if a is None or b is None else math.degrees(math.acos(min(1.0, float(a @ b))))
        r["speed"] = None if ang is None else ang / 2 / SPEED_W_S
    # jitter: back-and-forth of the mark between display updates (second difference: smooth motion cancels), over
    # consecutive same-glyph ticks while the wearer is not turning fast. Needs no perfectly still stretch.
    d2 = [
        float(np.linalg.norm(c["x"] - 2 * b["x"] + a["x"])) / math.sqrt(6)
        for a, b, c in zip(ok, ok[1:], ok[2:], strict=False)
        if a["chev"] == b["chev"] == c["chev"]
        and c["t"] - a["t"] < 1.0
        and all(q["speed"] is not None and q["speed"] < MOVE_DEG_S for q in (a, b, c))
    ]
    # lag: best time shift of the truth against the mark while moving (the chevron against its true edge point)
    mov = [r for r in ok if r["speed"] is not None and r["speed"] > MOVE_DEG_S]
    errs = []
    for s in SHIFTS_S:
        e = [
            float(np.linalg.norm(r["x"] - _goal(d, r["P"], r["chev"])))
            for r in mov
            if (d := _truth(raw, ts, r["t"] + r["rtt"] - s)) is not None
        ]
        errs.append(math.sqrt(np.mean(np.square(e))) if e else math.inf)
    steps = [float(np.linalg.norm(b["x"] - a["x"])) for a, b in itertools.pairwise(ok) if b["t"] - a["t"] < 1.0]
    flips = sum(a["chev"] != b["chev"] for a, b in itertools.pairwise(ok))
    minutes = max((ok[-1]["t"] - ok[0]["t"]) / 60, 1e-9)
    zero = int(np.argmin(np.abs(SHIFTS_S)))
    return {
        "ticks": len(ok),
        "minutes": round(minutes, 2),
        "jitter_px": round(math.sqrt(np.mean(np.square(d2))), 2) if d2 else None,
        "lag_ms": round(float(SHIFTS_S[int(np.argmin(errs))]) * 1000) if mov else None,
        "err_px": round(errs[zero], 1) if mov and math.isfinite(errs[zero]) else None,
        "flips_min": round(flips / minutes, 1),
        "max_step_px": round(max(steps), 1) if steps else 0.0,
        "lead_ms_mean": round(1000 * float(np.mean([r["lead"] for r in ok])), 0),
        "moving_ticks": len(mov),
    }


# (seconds, spoken prompt): front camera first, then the rear (Vision) path
SCRIPT = [
    (15, "Face the phone screen and look at the phone. Hold still."),
    (15, "Slowly turn your head left and right, keeping the ring in view."),
    (10, "Now turn fast, away from the phone and back."),
    (10, "Look at the phone and hold still."),
    (8, "Walk around behind the phone."),
    (15, "Look at the phone from behind it. Hold still."),
    (15, "Slowly turn your head left and right."),
    (10, "Now turn fast, away and back."),
    (10, "Look at the phone and hold still."),
]


def _api(path: str, method: str = "GET", **q) -> dict:
    url = f"http://127.0.0.1:{config.port}{path}" + (f"?{urllib.parse.urlencode(q)}" if q else "")
    with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=5) as r:
        return json.loads(r.read())


def _say(text: str) -> None:
    print(f"  {text}", flush=True)
    if shutil.which("say"):
        subprocess.Popen(["say", text])


def record() -> Path:
    """Guided capture against the running server (./scripts/dev.sh with BB_INTERACTIVE=1, phone app + glasses on):
    targets the phone, speaks the SCRIPT as suggestions, records until Ctrl-C, then saves just that window of the
    server's poses.jsonl as its own file."""
    while True:
        try:
            st = _api("/interactive/status")
        except OSError:
            missing = f"the server on port {config.port} with BB_INTERACTIVE=1 (./scripts/dev.sh)"
        else:
            sessions = list(st["display"])
            missing = ("the phone app" if not st["pose"]["app_connected"] else None) or (
                "the glasses" if not sessions else None
            )
            if not missing:
                break
        print(f"waiting for {missing} ...", flush=True)
        time.sleep(2)
    sn = sessions[0]
    _api(f"/interactive/step/{sn}", "POST", name=PHONE)
    _say("Recording. Press control C when you are done.")
    start = time.time()
    try:
        for secs, text in SCRIPT:
            _say(text)
            time.sleep(secs)
        _say("That was all the prompts. Keep going, or press control C.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    end = time.time()
    _api(f"/interactive/step/{sn}", "POST")
    _say("Saved.")
    src = config.data_dir / "diag" / "poses.jsonl"
    keep = [line for line in src.read_text().splitlines() if line and start <= json.loads(line)["now"] <= end]
    hello = next((line for line in src.read_text().splitlines() if json.loads(line)["k"] == "hello"), None)
    if hello and not any(json.loads(line)["k"] == "hello" for line in keep):
        keep.insert(0, hello)  # the app said hello before the window: replay needs its pose source
    out = src.with_name(time.strftime("run-%Y%m%d-%H%M%S.jsonl"))
    out.write_text("\n".join(keep) + "\n")
    print(f"saved {len(keep)} records to {out}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path", type=Path, nargs="?", default=Path("data/diag/poses.jsonl"))
    ap.add_argument("--record", action="store_true", help="record until Ctrl-C first (needs the running server)")
    ap.add_argument("--target", default=PHONE, help="object id to highlight (default: the phone)")
    ap.add_argument("--motion-tau", type=float)
    for c in ("SWAY_DEG_S", "TURN_DEG_S", "K_SIGMA", "SETTLE_TICKS"):
        ap.add_argument(f"--{c.lower().replace('_', '-')}", type=float, dest=c)
    a = ap.parse_args()
    consts = {c: v for c in ("SWAY_DEG_S", "TURN_DEG_S", "K_SIGMA", "SETTLE_TICKS") if (v := getattr(a, c)) is not None}
    path = record() if a.record else a.path
    head = {"motion_tau": a.motion_tau} if a.motion_tau is not None else None
    print(json.dumps(replay(load(path), a.target, head, **consts), indent=2))


if __name__ == "__main__":
    main()
