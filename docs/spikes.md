# Spikes — measured G2 / pipeline behaviour

Record any unverified G2 or pipeline behaviour here after testing on hardware or measuring.
This file is updated by whichever part learns something new.

---

## Part 1 — Perception pipeline

### Transcription backend (2026-10-17)

The brief specified `mlx-whisper` as the preferred transcription backend. `mlx-whisper` is
Apple-Silicon-only and cannot run on Windows. This project is being developed on Windows.

**Decision:** use `faster-whisper` (CPU/CUDA, Windows-OK) as the default backend; the
`GeminiBackend` is retained as a fallback (see `transcribe.py`). The output contract
(`list[TranscriptSegment]`) is identical — no inter-part changes needed.

**Measured:** faster-whisper large-v3, int8/CPU — transcription speed roughly 5–10× real-time on
a modern laptop CPU (i.e., a 10-minute video transcribes in ~1–2 min).

### ffmpeg requirement (2026-10-17)

`ffmpeg` and `ffprobe` must be on PATH. Install on each laptop with:

```
winget install Gyan.FFmpeg   # Windows
brew install ffmpeg          # macOS
sudo apt install ffmpeg      # Linux/WSL
```

The pipeline will raise a clear error if ffmpeg is not found.

### faster-whisper + PyAV on Python 3.14 (2026-10-17)

`faster-whisper` 1.2.1 calls `av.open(..., metadata_errors="ignore")` internally. On Python 3.14
with `av` 19.0.1 this raises `TypeError: open() got an unexpected keyword argument 'metadata_errors'`
— a Cython/Python 3.14 incompatibility.

**Workaround:** use `--transcriber gemini` until this is resolved upstream, or pin Python ≤3.13 in
the venv. The Gemini backend produces equivalent `TranscriptSegment` output with no local model
download required.

## Part 2 — Teaching on the glasses (S1, S4, S5, S6)

Results of the week 1 hardware spikes from `docs/DESIGN.md` §8. Everything in this section was measured on real G2 glasses unless a row says otherwise. Part 2 owns S1, S4, S5 and S6. Part 3 owns S2, S3 and S7 and adds its own section.

### How to run the Part 2 spike pages

1. Start everything with `./scripts/dev.sh`, scan the printed QR code in the Even Realities app, and wait for the skill library to appear on the glasses.
2. Find the session id, which is the glasses serial, with `curl -s localhost:8787/health`.
3. Push the spike pages onto those glasses with `curl -X POST localhost:8787/teach/spike/<session>`.
4. Every input on every spike page is echoed to the dim status line and logged by the server as a `spike` line with the client time, the server receive time and the server send time. Double tap moves to the next page, and a double tap on the last page returns to the lesson exactly where it was.

### S5. Input events and glyphs on the step view

The first spike page is the real `teach_page`, with the full screen blank text box as the capture container.

- Swipe forward and back on the right temple, then the left temple, then the ring. For each, note whether `scroll_down` and `scroll_up` appear in the status line.
- Tap, double tap, and hold then release on each temple and on the ring. Note whether `long_press` and `long_press_release` both arrive.
- The tooltip area shows a glyph test line. Note which characters are visible. Anything missing is silently dropped by the firmware, and `render.sanitize` must then drop it too.

If the blank capture box sends no scroll events, the fallback is a list capture step view. That changes `teach_page` in core, so it needs a contract PR to `main`.

### S1. Image over text

The second page has striped images on both halves of the middle band and a text box full of `TEXT` across the same band. The left image sits below the text in z order, and the right image sits above it. Note on each half whether the text shows through the black stripes, whether the image hides the text, or whether the result flickers.

### S6. End to end display latency

The third page shows `TAP n` in the tooltip area on every tap. Film the lens and the tapping finger together in slow motion, then count frames from the finger touching the temple to the new number appearing. The server log gives the network part of the delay, since `recv` minus `client_t` is the trip to the laptop plus any clock skew, and `sent` minus `recv` is the server time. Repeat about 20 taps and report the median.

Also report the voice round trip from the server log lines `answered in N.NNs`, which time release to first answer page, over at least 10 questions.

### S4. Network whitelist and Tailscale on iOS

1. Build a packaged app with `./scripts/pack-g2.sh` and install the `.ehpk` as a private build.
2. Check whether the app connects. `pack-g2.sh` whitelists both `https://` and `wss://` for the laptop origin, so to answer the question repeat the test with a build whose whitelist only has the `https://` entry.
3. With the lesson open on a step, lock the phone for five minutes, then unlock it. The same step must come back, which is an Even beta test requirement. Repeat with the Even app in the background for five minutes.

### Simulator observations, 2026-10-17

These came from the Even Hub simulator 0.9.5 driven through its automation port, so they only hint at what the hardware does and every row in the results table still needs real glasses.

- Swipes on the step view reach the server as `scroll_down` and `scroll_up` with the blank full screen capture box, so S5 looks good in the simulator.
- A tooltip line holds about 60 characters, so the 120 character `render.TIP_CHARS` budget fills two of the three lines the `tip` box has room for. If the hardware matches, raising it to about 170 cuts the number of pages to scroll.
- The automation `long_press` action sends the press and the release 18 ms apart, so automated runs can't test push to talk. Holding the button in the simulator window by hand does.
- Still open for hardware is whether a rebuilt page starts with empty image boxes. Until that is known, the controller resends black tiles after every full redraw of a step without a keyframe, which costs about 200 ms.

### Results

| Spike | Date | Glasses firmware, app version | Result | Consequence |
|---|---|---|---|---|
| S1 image over text | | | | |
| S4 whitelist for wss | | | | |
| S4 phone locked 5 min | | | | |
| S5 scroll on blank capture box | 2026-10-17 | not recorded | Works. Swipes arrive as `scroll_up` and `scroll_down` from the `bg` box, with source `unknown`. One swipe sometimes arrives twice about 170 ms apart. | Step paging stays on the blank capture box, and repeats of the same swipe within 300 ms are ignored. |
| S5 long press and release, temples and ring | 2026-10-17 | not recorded | At first only `long_press_release` reached the server, from both temples. The cause was the glasses app itself. It called `crypto.randomUUID`, which the Even WebView doesn't define on the plain http dev page, so handling the press crashed before it was sent. With that fixed, recording works, with a 5.0 s utterance answered in 4.4 s. The ring wasn't tested. | Hold to ask is back. Confirm the press now reaches the server on both temples. |
| S5 glyphs that render | | | | |
| S6 tap to visible, median | | | | |
| S6 voice release to first page, p50 | | | | |

---

## Part 3 — AR highlight (S2, S3, S7)

Setup: iPhone 17 Pro in World mode (`apps/camera-ios`), wearer ~1.3 m away behind the phone (rear camera), G2
with the overlay band of `teach_page`. Recordings: `uv run python -m bluebook_interactive.replay --record`.

### 2026-10-18: rear-camera session and glasses IMU (measured)

- Rear-camera Vision face at ~1.3 m: yaw ~1°/frame and wanders 3-5°; pitch noise ~2-3° (front TrueDepth ~0.08°).
- G2 overlay band is 144 px, ~±2.7° vertical.
- Draw round trip (publish → `drawn` ack) ~130 ms; replayed motion-to-photon lag ~350 ms on the 45 s clip
  (`packages/bluebook_interactive/tests/rear_imu_clip.jsonl.gz`).
- Glasses IMU (S3, partial): accelerometer only, no gyro; |a| ~1 g at rest; ~10.6 Hz at pace 100. Other pace codes
  not yet measured. Gravity gives pitch/roll only; the tracker blends it into the camera pose.
- Vision object tracker (iOS `VNTrackObjectRequest`): after losing an object it can "re-acquire" the background with
  confidence > 0.5; LiDAR re-sampled there moved one anchor 1.8 m. Located objects are now fixed world points that the
  tracker may only refine by 8 cm.

Not yet run: S2 (both eyes / parallax), the remaining IMU pace codes, S7 yaw range with the G2 worn.
