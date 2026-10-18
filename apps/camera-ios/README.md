# Bluebook Camera (iOS)

Native replacement for the Safari page `apps/camera`. Streams upright JPEG frames to `/ws/camera` and head pose
(ARKit face tracking on the front camera, otherwise Apple Vision) plus Vision target tracks to
`/interactive/ws/pose`. Any lens, any zoom. No third-party packages.

## Install on your iPhone

```bash
brew install xcodegen          # once
cd apps/camera-ios && xcodegen # generates BluebookCamera.xcodeproj (gitignored)
open BluebookCamera.xcodeproj
```

1. Plug in the iPhone, unlock it, tap Trust. Enable Settings > Privacy & Security > Developer Mode (restart).
2. In Xcode pick the iPhone as run destination (team 3Y435S5YAR is preconfigured, automatic signing) and press Run.
3. First launch: Settings > General > VPN & Device Management > trust the developer; allow Camera and Local Network.
4. Tap the gear, enter the server URL printed by `./scripts/dev.sh` (e.g. `https://<laptop>.<tailnet>.ts.net`; `http://` LAN works too), then Start.

Tests (head-on convention check): `xcodebuild -scheme BluebookCamera -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test`.

## Conventions (sent on the wire)

Camera frame = OpenCV (x right, y down, z forward, metres) on the upright, unmirrored JPEG. Head frame: origin at the
eye midpoint, +x wearer's left, +y up, +z out of the face; `X_cam = R·X_head + t`. Head-on: `R = diag(1,-1,-1)`.
The preview draws the projected head axes (red x, green y, blue z) and eye midpoint: use them to eyeball a wrong sign.

`NSAllowsArbitraryLoads` is on in Info.plist so plain `http`/`ws` LAN servers work. Dev tool only.

## World mode (default when supported)

`ARWorldTrackingConfiguration` on the rear camera + `userFaceTrackingEnabled` (front TrueDepth face in the same metric,
gravity-aligned world) + LiDAR scene depth/mesh. Pose messages (source `arkit-world`, raw ARKit world values, no mirroring)
carry `face {R,t,conf}` (head->world), `anchors [{id,p,how}]` (always incl. `camera`), `cam_T` (row-major camera->world) and
`errors`. Server -> app on the pose WS: `locate {id,box}` (5x5 grid in the central 50% of the box, LiDAR depth else raycast,
robust median -> ARAnchor named id), `forget {id}`. Tap the preview to place `tapped` (POST `/interactive/step/<session>?anchor=tapped&name=...`).
"Restore last scene" reloads the saved ARWorldMap + anchors. Modes: World | Face (ARKit) | Vision.

Pose messages in World mode also carry `ar: {tracking: "normal"|"limited:<reason>"|"notAvailable", faces, face_tracked, face_age_s|null, fps}`.
The face used is always the newest *tracked* `ARFaceAnchor` in `frame.anchors` (ARKit may replace the anchor with a new identifier).
Video format: the default if it is the rear wide camera, else the largest wide format (ARKit has no per-format face-tracking query).
The cube button shows a TOP / SIDE schematic: phone (orange = rear look direction, cyan = front camera), head axes (red x = face's left,
green y = up, blue z = out), yellow G2 display wedge, dashed links to the phone and anchors. TOP: x right, work area (-z) up. SIDE: work area (-z) right, y up.

## Rates

Pose JSON goes out for every processed frame (ARKit: 60 Hz delivery throttled to 30 Hz; Vision: 1280x720 binned format locked to 30 fps, one frame in flight). JPEGs to `/ws/camera` use a separate queue at the Settings rate (default 10 fps, skipped when busy). The status row shows measured pose Hz, JPEG Hz and processing ms/frame. The overlay redraws at ~20 Hz. Setting changes restart the sockets immediately while streaming.
