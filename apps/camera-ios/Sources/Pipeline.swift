import CoreImage
import Foundation
import ImageIO
import Vision
import simd

struct AnchorInfo: Sendable, Identifiable {
    var id: String; var how: String; var dist: Float
    var pt: CGPoint?  // normalised in the upright image; nil when behind the camera / off-image
}

struct TrackBox: Sendable { var id: String; var box: CGRect; var conf: Float; var held = false }  // box normalised, top-left origin

/// What the UI needs per processed frame (all normalised 0..1 in the upright sent image).
struct FrameReport: Sendable {
    var imageSize = CGSize(width: 9, height: 16)
    var poseHz: Double = 0
    var jpegHz: Double = 0
    var procMs: Double = 0  // mean pose-processing time per frame
    var hasFace = false
    var faceBox: CGRect?
    var eye: CGPoint?
    var axes: [CGPoint] = []  // origin, +x, +y, +z tips
    var tracks: [TrackBox] = []
    var anchors: [AnchorInfo] = []  // world mode only (excludes the phone itself)
    var scene: SceneSnapshot?  // world mode only: feeds the schematic
    var ar: ARDiag?  // world mode only
}

enum PoseSource: String, Sendable { case arkit, vision, world = "arkit-world" }

/// Everything that happens to a frame: face pose (Vision path), target tracking, JPEG + pose messages, preview.
/// All frame entry points must be called on `queue`.
final class Pipeline: @unchecked Sendable {
    let queue = DispatchQueue(label: "bluebook.pipeline", qos: .userInitiated)
    let camSocket = WSClient(maxPending: 3)
    let poseSocket = WSClient(maxPending: 10)
    var onReport: (@Sendable (FrameReport) -> Void)?
    var onPreview: (@Sendable (CGImage) -> Void)?
    var world: WorldEngine?  // set once at init; used on `queue`

    private var source = PoseSource.vision
    private var ipd: Float = 0.063
    private var sendInterval = 0.1
    private var cameraId = "scene"
    private var device = ""
    private let maxSide: CGFloat = 1280
    private let ci = CIContext()  // used only on imageQueue
    private let seq = VNSequenceRequestHandler()  // face + trackers, used only on queue
    private let faceReq = VNDetectFaceLandmarksRequest()  // reused every frame
    // Head angles MUST come from face rectangles revision 3: earlier revisions (and the landmarks request's own
    // face pass) report roll/yaw in discrete bins -> measured ~48° snaps on the back camera (2026-10-17).
    // Revision 3 reports roll, yaw AND pitch continuously.
    private let poseReq: VNDetectFaceRectanglesRequest = {
        let r = VNDetectFaceRectanglesRequest()
        r.revision = VNDetectFaceRectanglesRequestRevision3
        return r
    }()
    /// JPEG encode + preview CGImage run here, never on `queue`. At most one job in flight; if busy the frame is skipped.
    private let imageQueue = DispatchQueue(label: "bluebook.image", qos: .utility)
    private let imgLock = NSLock()
    private var imgBusy = false, jpegCount = 0
    private let previewInterval = 1 / 20.0  // preview image + overlay report rate
    private var trackers: [String: VNDetectedObjectObservation] = [:]
    private var lastSize = CGSize.zero
    private var lastSend = 0.0, lastReport = 0.0, lastPreview = 0.0
    private var frames = 0, windowStart = 0.0, poseHz = 0.0, jpegHz = 0.0, procMs = 0.0, procSum = 0.0

    func configure(source: PoseSource, device: String, server: String) {
        queue.async { [self] in
            self.source = source; self.device = device
            applyConfig(server: server)
        }
    }

    /// Re-read settings (server, camera id, IPD, JPEG rate) and restart the sockets; keeps source/device.
    func reload(server: String) { queue.async { [self] in applyConfig(server: server) } }

    private func applyConfig(server: String) {
        ipd = Settings.ipd; sendInterval = 1 / max(Settings.sendFPS, 1); cameraId = Settings.cameraId
            lastSize = .zero
            camSocket.start(url: Settings.wsURL(server, path: "/ws/camera"))
            poseSocket.start(url: Settings.wsURL(server, path: "/interactive/ws/pose"))
            poseSocket.onText = { [weak self] s in self?.queue.async { self?.handleServer(s) } }
            poseSocket.setHello(json(["type": "pose_hello", "camera_id": cameraId, "source": source.rawValue, "device": device]))
    }

    func stop() {
        camSocket.stop(); poseSocket.stop()
        queue.async { [self] in trackers = [:]; lastSize = .zero; windowStart = 0; frames = 0; procSum = 0 }
    }

    // MARK: server -> app

    private func handleServer(_ s: String) {
        guard let o = try? JSONSerialization.jsonObject(with: Data(s.utf8)) as? [String: Any],
              let id = o["id"] as? String else { return }
        switch o["type"] as? String {
        case "track":
            guard let b = (o["box"] as? [Any])?.compactMap({ ($0 as? NSNumber)?.doubleValue }), b.count == 4 else { return }
            // normalised top-left -> Vision normalised bottom-left
            let r = CGRect(x: b[0], y: 1 - b[3], width: max(b[2] - b[0], 0.005), height: max(b[3] - b[1], 0.005))
            trackers[id] = VNDetectedObjectObservation(boundingBox: r)  // new box for same id re-inits the tracker
        case "untrack": trackers[id] = nil
        case "locate":
            guard let b = (o["box"] as? [Any])?.compactMap({ ($0 as? NSNumber)?.doubleValue }), b.count == 4 else { return }
            world?.locate(id: id, box: CGRect(x: b[0], y: b[1], width: max(b[2] - b[0], 0.005), height: max(b[3] - b[1], 0.005)))
        case "forget": world?.forget(id)
        default: break
        }
    }

    // MARK: frame entry points

    /// AVCapture frame: upright BGRA buffer, K already in its pixel frame (zoom applied).
    func processCapture(_ pb: CVPixelBuffer, t: Double, K: Intrinsics) {
        let t0 = ProcessInfo.processInfo.systemUptime
        let size = CGSize(width: CVPixelBufferGetWidth(pb), height: CVPixelBufferGetHeight(pb))
        var face: HeadPose?, box: CGRect?, conf: Float = 0
        let req = faceReq
        // 1) continuous angles (rev 3)  2) landmarks on that same face (inputFaceObservations)
        if (try? seq.perform([poseReq], on: pb, orientation: .up)) != nil,
           let poseObs = (poseReq.results ?? []).max(by: { $0.boundingBox.area < $1.boundingBox.area }),
           { req.inputFaceObservations = [poseObs]; return true }(),
           (try? seq.perform([req], on: pb, orientation: .up)) != nil,
           let obs = req.results?.first {
            box = CGRect(x: obs.boundingBox.minX, y: 1 - obs.boundingBox.maxY,
                         width: obs.boundingBox.width, height: obs.boundingBox.height)
            conf = obs.landmarks?.confidence ?? obs.confidence
            if let lm = obs.landmarks, let roll = poseObs.roll?.floatValue, let yaw = poseObs.yaw?.floatValue {
                let pitch = poseObs.pitch?.floatValue ?? 0
                // Vision points are pixels with a bottom-left origin; flip to top-left.
                func pt(_ r: VNFaceLandmarkRegion2D?) -> CGPoint? {
                    guard let r, r.pointCount > 0 else { return nil }
                    let p = r.pointsInImage(imageSize: size)
                    let c = p.reduce(CGPoint.zero) { CGPoint(x: $0.x + $1.x, y: $0.y + $1.y) }
                    return CGPoint(x: c.x / CGFloat(p.count), y: size.height - c.y / CGFloat(p.count))
                }
                if let l = pt(lm.leftPupil) ?? pt(lm.leftEye), let r = pt(lm.rightPupil) ?? pt(lm.rightEye),
                   let tt = Geo.position(left: l, right: r, K: K, yaw: yaw, ipd: ipd) {
                    face = HeadPose(R: Geo.rotation(roll: roll, yaw: yaw, pitch: pitch), t: tt)
                }
            }
        }
        finish(pb, .up, size, t, K, face, conf, box, t0)
    }

    
    /// ARKit frame: buffer is sensor-landscape; everything is reported in the portrait (rotated .right) frame.
    func processAR(_ frame: ARFrameLite) {
        let t0 = ProcessInfo.processInfo.systemUptime
        let pb = frame.pixelBuffer
        let size = CGSize(width: CVPixelBufferGetHeight(pb), height: CVPixelBufferGetWidth(pb))
        let K = Geo.arkitIntrinsics(frame.intrinsics, landscapeH: Float(CVPixelBufferGetHeight(pb)))
        var pose: HeadPose?
        if let f = frame.face { pose = Geo.arkitPose(camT: frame.cameraTransform, faceT: f.transform, leftEye: f.left, rightEye: f.right) }
        finish(pb, .right, size, frame.captureTime, K, pose, pose == nil ? 0 : 1, nil, t0)
    }

    /// World-tracking frame (rear camera): head/anchors are already in the ARKit world frame; reported raw.
    func processWorld(_ w: WorldFrame) {
        let t0 = ProcessInfo.processInfo.systemUptime
        let pb = w.pixelBuffer
        let (lw, lh) = (Float(CVPixelBufferGetWidth(pb)), Float(CVPixelBufferGetHeight(pb)))
        let size = CGSize(width: CGFloat(lh), height: CGFloat(lw))
        let K = Geo.arkitIntrinsics(w.intrinsics, landscapeH: lh)
        let KL = Intrinsics(fx: w.intrinsics[0][0], fy: w.intrinsics[1][1], cx: w.intrinsics[2][0], cy: w.intrinsics[2][1])
        let cam = w.cameraTransform.columns.3.xyz
        let infos: [AnchorInfo] = w.anchors.map { a in
            var pt: CGPoint?
            if let q = Geo.projectARKit(a.p, K: KL, camT: w.cameraTransform) {
                let n = Geo.portraitNorm(fromLandscape: CGPoint(x: Double(q.u / lw), y: Double(q.v / lh)))
                if (0...1).contains(n.x) && (0...1).contains(n.y) { pt = n }
            }
            return AnchorInfo(id: a.id, how: a.how, dist: simd_distance(a.p, cam), pt: pt)
        }
        finish(pb, .right, size, w.captureTime, K, nil, w.head == nil ? 0 : 1, nil, t0, world: (w, infos),
               sceneK: (KL, CGSize(width: CGFloat(lw), height: CGFloat(lh))))
    }

    // MARK: common

    private func finish(_ pb: CVPixelBuffer, _ o: CGImagePropertyOrientation, _ size: CGSize, _ t: Double,
                        _ K: Intrinsics, _ face: HeadPose?, _ conf: Float, _ faceBox: CGRect?, _ t0: Double,
                        world: (frame: WorldFrame, infos: [AnchorInfo])? = nil,
                        sceneK: (Intrinsics, CGSize)? = nil) {
        let now = ProcessInfo.processInfo.systemUptime
        let s = min(1, maxSide / max(size.width, size.height))
        let sent = CGSize(width: (size.width * s).rounded(), height: (size.height * s).rounded())
        if sent != lastSize {  // resolution/orientation changed: the camera hello must be re-sent
            lastSize = sent
            camSocket.setHello(json(["type": "camera_hello", "camera_id": cameraId, "width": Int(sent.width),
                                     "height": Int(sent.height), "fps": 1 / sendInterval, "device": device]))
        }

        let tracks = runTrackers(pb, o)

        // pose message: one per processed frame
        let ks = K.scaled(Float(s))
        var msg: [String: Any] = ["type": "pose", "t": t, "w": Int(sent.width), "h": Int(sent.height),
                                  "K": [ks.fx, ks.fy, ks.cx, ks.cy].map { Double($0) },
                                  "face": NSNull(),
                                  "tracks": tracks.map { ["id": $0.id, "box": [$0.box.minX, $0.box.minY, $0.box.maxX, $0.box.maxY],
                                                         "conf": $0.conf, "z": NSNull()] as [String: Any] }]
        if let face { msg["face"] = ["R": Geo.rowMajor(face.R).map { Double($0) }, "t": [Double(face.t.x), Double(face.t.y), Double(face.t.z)], "conf": Double(conf)] }
        if let c = face?.camInHead { msg["cam_in_head"] = [Double(c.x), Double(c.y), Double(c.z)] }
        var errKeys: [String] = []
        if let (w, _) = world {
            msg["cam_T"] = Geo.rowMajor(w.cameraTransform).map { Double($0) }
            msg["ar"] = w.ar.json
            let cam = w.cameraTransform.columns.3.xyz
            msg["anchors"] = ([WorldAnchor(id: "camera", p: cam, how: "camera")] + w.anchors).map(\.json)
            if let h = w.head {
                msg["face"] = ["R": Geo.rowMajor(h.R).map { Double($0) }, "t": [Double(h.t.x), Double(h.t.y), Double(h.t.z)], "conf": Double(h.conf), "src": h.src.rawValue]
            }
            let errs = self.world?.pendingErrors() ?? [:]
            if !errs.isEmpty { msg["errors"] = errs; errKeys = Array(errs.keys) }
        }
        if poseSocket.send(text: json(msg)) { self.world?.clearErrors(errKeys) }

        // JPEG / preview: off this queue, skipped if the image queue is still busy
        let wantJPEG = now - lastSend >= sendInterval, wantPreview = now - lastPreview >= previewInterval
        if (wantJPEG || wantPreview) && beginImageJob() {
            if wantJPEG { lastSend = now }
            if wantPreview { lastPreview = now }
            let buf = PixelBuffer(pb)
            imageQueue.async { [self] in
                let img = CIImage(cvPixelBuffer: buf.pb).oriented(o).transformed(by: CGAffineTransform(scaleX: s, y: s))
                if wantJPEG, let d = ci.jpegRepresentation(of: img, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                                                           options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.7]),
                   camSocket.send(data: d) {
                    imgLock.withLock { jpegCount += 1 }
                }
                if wantPreview, let cg = ci.createCGImage(img, from: img.extent) { onPreview?(cg) }
                imgLock.withLock { imgBusy = false }
            }
        }

        // measured rates
        let done = ProcessInfo.processInfo.systemUptime
        frames += 1; procSum += done - t0
        if done - windowStart >= 1 {
            let dt = done - windowStart
            poseHz = Double(frames) / dt
            procMs = procSum / Double(max(frames, 1)) * 1000
            jpegHz = Double(imgLock.withLock { let c = jpegCount; jpegCount = 0; return c }) / dt
            frames = 0; procSum = 0; windowStart = done
        }

        if now - lastReport >= previewInterval {
            lastReport = now
            var r = FrameReport(imageSize: size, poseHz: poseHz, jpegHz: jpegHz, procMs: procMs, hasFace: face != nil || world?.frame.head != nil, tracks: tracks)
            r.faceBox = faceBox
            r.anchors = world?.infos ?? []
            // coupled anchors: draw their tracker box (cyan while tracked, orange while held)
            r.tracks += (world?.frame.anchors ?? []).compactMap { a in
                a.box.map { TrackBox(id: a.id, box: $0, conf: a.conf, held: a.how == "held") }
            }
            if let w = world?.frame { r.scene = SceneSnapshot(head: w.head, camT: w.cameraTransform, anchors: w.anchors, K: sceneK?.0, landscape: sceneK?.1 ?? .zero); r.ar = w.ar }
            if let face {
                func n(_ p: CGPoint?) -> CGPoint? { p.map { CGPoint(x: $0.x / size.width, y: $0.y / size.height) } }
                let pts = [[0, 0, 0], [0.06, 0, 0], [0, 0.06, 0], [0, 0, 0.06]].map { Geo.project($0, face, K) }
                r.eye = n(pts[0])
                if pts.allSatisfy({ $0 != nil }) { r.axes = pts.compactMap { n($0) } }
            }
            onReport?(r)
        }
    }

    private func beginImageJob() -> Bool {
        imgLock.withLock { if imgBusy { return false }; imgBusy = true; return true }
    }

    private func runTrackers(_ pb: CVPixelBuffer, _ o: CGImagePropertyOrientation) -> [TrackBox] {
        guard !trackers.isEmpty else { return [] }
        let reqs = trackers.map { (id: $0.key, req: VNTrackObjectRequest(detectedObjectObservation: $0.value)) }
        try? seq.perform(reqs.map(\.req), on: pb, orientation: o)
        return reqs.map { id, req in
            if let r = req.results?.first as? VNDetectedObjectObservation {
                trackers[id] = r
                return trackBox(id, r.boundingBox, r.confidence)
            }
            return trackBox(id, trackers[id]!.boundingBox, 0)  // lost: report last box with conf 0
        }
    }

    private func trackBox(_ id: String, _ b: CGRect, _ c: Float) -> TrackBox {
        TrackBox(id: id, box: CGRect(x: b.minX, y: 1 - b.maxY, width: b.width, height: b.height), conf: c)
    }

    private func json(_ o: [String: Any]) -> String {
        String(decoding: (try? JSONSerialization.data(withJSONObject: o)) ?? Data(), as: UTF8.self)
    }
}

/// CVPixelBuffer handed to the image queue.
struct PixelBuffer: @unchecked Sendable { let pb: CVPixelBuffer; init(_ pb: CVPixelBuffer) { self.pb = pb } }

extension CGRect { var area: CGFloat { width * height } }

/// The bits of ARFrame the pipeline needs (so the pipeline doesn't depend on ARKit types).
struct ARFrameLite: @unchecked Sendable {
    struct Face { var transform: simd_float4x4; var left: simd_float4x4; var right: simd_float4x4 }
    var pixelBuffer: CVPixelBuffer
    var intrinsics: simd_float3x3
    var cameraTransform: simd_float4x4
    var captureTime: Double  // unix epoch seconds
    var face: Face?
}
