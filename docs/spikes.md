# Hardware spikes

Results of the week 1 hardware spikes from `docs/DESIGN.md` §8. Everything in this file was measured on real G2 glasses unless a row says otherwise. Part 2 owns S1, S4, S5 and S6. Part 3 owns S2, S3 and S7 and adds its sections below.

## How to run the Part 2 spike pages

1. Start everything with `./scripts/dev.sh`, scan the printed QR code in the Even Realities app, and wait for the skill library to appear on the glasses.
2. Find the session id, which is the glasses serial, with `curl -s localhost:8787/health`.
3. Push the spike pages onto those glasses with `curl -X POST localhost:8787/teach/spike/<session>`.
4. Every input on every spike page is echoed to the dim status line and logged by the server as a `spike` line with the client time, the server receive time and the server send time. Double tap moves to the next page, and a double tap on the last page returns to the lesson exactly where it was.

## S5. Input events and glyphs on the step view

The first spike page is the real `teach_page`, with the full screen blank text box as the capture container.

- Swipe forward and back on the right temple, then the left temple, then the ring. For each, note whether `scroll_down` and `scroll_up` appear in the status line.
- Tap, double tap, and hold then release on each temple and on the ring. Note whether `long_press` and `long_press_release` both arrive.
- The tooltip area shows a glyph test line. Note which characters are visible. Anything missing is silently dropped by the firmware, and `render.sanitize` must then drop it too.

If the blank capture box sends no scroll events, the fallback is a list capture step view. That changes `teach_page` in core, so it needs a contract PR to `main`.

## S1. Image over text

The second page has striped images on both halves of the middle band and a text box full of `TEXT` across the same band. The left image sits below the text in z order, and the right image sits above it. Note on each half whether the text shows through the black stripes, whether the image hides the text, or whether the result flickers.

## S6. End to end display latency

The third page shows `TAP n` in the tooltip area on every tap. Film the lens and the tapping finger together in slow motion, then count frames from the finger touching the temple to the new number appearing. The server log gives the network part of the delay, since `recv` minus `client_t` is the trip to the laptop plus any clock skew, and `sent` minus `recv` is the server time. Repeat about 20 taps and report the median.

Also report the voice round trip from the server log lines `answered in N.NNs`, which time release to first answer page, over at least 10 questions.

## S4. Network whitelist and Tailscale on iOS

1. Build a packaged app with `./scripts/pack-g2.sh` and install the `.ehpk` as a private build.
2. Check whether the app connects. `pack-g2.sh` whitelists both `https://` and `wss://` for the laptop origin, so to answer the question repeat the test with a build whose whitelist only has the `https://` entry.
3. With the lesson open on a step, lock the phone for five minutes, then unlock it. The same step must come back, which is an Even beta test requirement. Repeat with the Even app in the background for five minutes.

## Simulator observations, 2026-10-17

These came from the Even Hub simulator 0.9.5 driven through its automation port, so they only hint at what the hardware does and every row in the results table still needs real glasses.

- Swipes on the step view reach the server as `scroll_down` and `scroll_up` with the blank full screen capture box, so S5 looks good in the simulator.
- A tooltip line holds about 60 characters, so the 120 character `render.TIP_CHARS` budget fills two of the three lines the `tip` box has room for. If the hardware matches, raising it to about 170 cuts the number of pages to scroll.
- The automation `long_press` action sends the press and the release 18 ms apart, so automated runs can't test push to talk. Holding the button in the simulator window by hand does.
- Still open for hardware is whether a rebuilt page starts with empty image boxes. Until that is known, the controller resends black tiles after every full redraw of a step without a keyframe, which costs about 200 ms.

## Results

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
