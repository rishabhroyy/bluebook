import ARKit
import os
@preconcurrency import AVFoundation
import Observation
import SwiftUI
import UIKit

struct Lens: Identifiable, Hashable {
    let id: String  // AVCaptureDevice.uniqueID
    let label: String  // "0.5×"
    let name: String  // "ultrawide"
    let base: Double  // field-of-view ratio vs. the main wide camera
}

/// Unix-epoch seconds for a timestamp on the system-uptime clock (CMSampleBuffer pts and ARFrame.timestamp).
func epoch(fromUptime s: Double) -> Double { Date().timeIntervalSince1970 - (ProcessInfo.processInfo.systemUptime - s) }

/// Owns the AVCaptureSession (Vision path) and the ARSession (ARKit path); feeds frames to the pipeline.
final class Rig: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate, ARSessionDelegate, @unchecked Sendable {
    let pipeline: Pipeline
    private let capture = AVCaptureSession()
    private let ar = ARSession()
    private let sessionQueue = DispatchQueue(label: "bluebook.session")
    private var device: AVCaptureDevice?
    private var lastAR = 0.0
    private var loggedZoom = false
    let world: WorldEngine
    nonisolated(unsafe) private var worldMode = false  // written on sessionQueue, read on the delegate queue
    private let log = Logger(subsystem: "com.rishabhroy.bluebook.camera", category: "ar")
    /// Surfaced in the UI: interruptions / failures / lost tracking (nil = fine). Called on the pipeline queue.
    var onStatus: (@Sendable (String?) -> Void)?
    nonisolated(unsafe) private var lastConfig: ARConfiguration?  // sessionQueue; re-run (no reset) after a failure
    // Delegate-queue state (pipeline.queue): face selection, fps window, last logged state.
    private var faceBook = FaceBook()
    private var faceSel = FaceSourceSelector()
    private let rearFace = RearFace()
    private var fpsStart = 0.0, fpsCount = 0, fps = 0.0
    private var lastLogged = ""
    private var statusSet = false

    init(pipeline: Pipeline) {
        self.pipeline = pipeline
        world = WorldEngine(session: ar, queue: pipeline.queue)
        super.init()
        pipeline.world = world
        ar.delegate = self
        ar.delegateQueue = pipeline.queue
    }

    func startCapture(_ dev: AVCaptureDevice) {
        sessionQueue.async { [self] in
            worldMode = false
            ar.pause()
            capture.beginConfiguration()
            capture.inputs.forEach(capture.removeInput)
            capture.outputs.forEach(capture.removeOutput)
            capture.sessionPreset = .hd1280x720
            if let input = try? AVCaptureDeviceInput(device: dev), capture.canAddInput(input) { capture.addInput(input) }
            Self.pickFormat(dev)
            let out = AVCaptureVideoDataOutput()
            out.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarFullRange]
            out.alwaysDiscardsLateVideoFrames = true
            out.setSampleBufferDelegate(self, queue: pipeline.queue)
            if capture.canAddOutput(out) { capture.addOutput(out) }
            if let c = out.connection(with: .video) {
                if c.isVideoRotationAngleSupported(90) { c.videoRotationAngle = 90 }  // upright portrait
                if c.isVideoMirroringSupported { c.automaticallyAdjustsVideoMirroring = false; c.isVideoMirrored = false }
                if c.isCameraIntrinsicMatrixDeliverySupported { c.isCameraIntrinsicMatrixDeliveryEnabled = true }
            }
            capture.commitConfiguration()
            device = dev
            if !capture.isRunning { capture.startRunning() }
        }
    }

    /// 16:9, ~1280 wide, binned (low latency), able to run 30 fps; then lock the frame rate to 30.
    private static func pickFormat(_ d: AVCaptureDevice) {
        func dims(_ f: AVCaptureDevice.Format) -> CMVideoDimensions { CMVideoFormatDescriptionGetDimensions(f.formatDescription) }
        let best = d.formats.filter { f in
            let s = dims(f)
            return s.width >= 1280 && s.width <= 1920 && s.width * 9 == s.height * 16
                && f.videoSupportedFrameRateRanges.contains { $0.minFrameRate <= 30 && $0.maxFrameRate >= 30 }
        }.min { a, b in
            (abs(Int(dims(a).width) - 1280), a.isVideoBinned ? 0 : 1) < (abs(Int(dims(b).width) - 1280), b.isVideoBinned ? 0 : 1)
        }
        guard let best, (try? d.lockForConfiguration()) != nil else { return }
        d.activeFormat = best
        let dur = CMTime(value: 1, timescale: 30)
        d.activeVideoMinFrameDuration = dur; d.activeVideoMaxFrameDuration = dur
        d.unlockForConfiguration()
    }

    func startAR() {
        sessionQueue.async { [self] in
            if capture.isRunning { capture.stopRunning() }
            device = nil
            worldMode = false
            let c = ARFaceTrackingConfiguration()
            c.maximumNumberOfTrackedFaces = 1
            lastConfig = c
            pipeline.queue.sync { faceBook.reset(); faceSel.reset(); rearFace.reset() }
            ar.run(c, options: [.resetTracking, .removeExistingAnchors])
        }
    }

    static var worldSupported: Bool { ARWorldTrackingConfiguration.isSupported && ARWorldTrackingConfiguration.supportsUserFaceTracking }

    /// Rear-camera world tracking + front-camera user face tracking + LiDAR depth/mesh where available.
    func startWorld(restore: Bool) {
        sessionQueue.async { [self] in
            if capture.isRunning { capture.stopRunning() }
            device = nil
            let c = ARWorldTrackingConfiguration()
            // Face tracking stays on whatever the world tracking state is; nothing here restarts the session on face loss.
            // (maximumNumberOfTrackedFaces exists only on ARFaceTrackingConfiguration; user face tracking is one face.)
            c.userFaceTrackingEnabled = ARWorldTrackingConfiguration.supportsUserFaceTracking
            c.videoFormat = Self.worldFormat()
            log.info("world format \(c.videoFormat.imageResolution.width, privacy: .public)x\(c.videoFormat.imageResolution.height, privacy: .public) @\(c.videoFormat.framesPerSecond, privacy: .public) \(c.videoFormat.captureDeviceType.rawValue, privacy: .public) userFace=\(c.userFaceTrackingEnabled, privacy: .public)")
            var sem: ARConfiguration.FrameSemantics = []
            for f: ARConfiguration.FrameSemantics in [.sceneDepth, .smoothedSceneDepth] where ARWorldTrackingConfiguration.supportsFrameSemantics(f) { sem.insert(f) }
            c.frameSemantics = sem
            let mesh = ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh)
            if mesh { c.sceneReconstruction = .mesh }
            c.planeDetection = [.horizontal, .vertical]
            c.environmentTexturing = .none
            c.worldAlignment = .gravity
            var how: [String: String] = [:]
            if restore, let saved = WorldStore.load() { c.initialWorldMap = saved.map; how = saved.how }
            lastConfig = c
            pipeline.queue.sync { world.reset(meshOn: mesh, savedHow: how); faceBook.reset(); faceSel.reset(); rearFace.reset(); fpsStart = 0; fps = 0 }
            worldMode = true
            ar.run(c, options: [.resetTracking, .removeExistingAnchors])
        }
    }

    /// Video format for world tracking. No ARKit API reports which formats support user face tracking, so use the one the
    /// front camera is known to run alongside: the rear WIDE camera (never ultra-wide / tele, which can starve the TrueDepth
    /// stream). Keep the system default when it is already wide; otherwise the largest wide format.
    static func worldFormat() -> ARWorldTrackingConfiguration.VideoFormat {
        let def = ARWorldTrackingConfiguration().videoFormat
        if def.captureDeviceType == .builtInWideAngleCamera { return def }
        return ARWorldTrackingConfiguration.supportedVideoFormats
            .filter { $0.captureDeviceType == .builtInWideAngleCamera && $0.framesPerSecond >= 30 }
            .max { $0.imageResolution.width < $1.imageResolution.width } ?? def
    }

    /// Persist the ARWorldMap (+ anchor kinds). `thenPause`: stop the session afterwards.
    func saveWorldMap(thenPause: Bool = false) {
        sessionQueue.async { [self] in
            guard worldMode else { if thenPause { ar.pause() }; return }
            ar.getCurrentWorldMap { [self] map, _ in
                if let map { WorldStore.save(map, how: world.howSnapshot()) }
                if thenPause { worldMode = false; ar.pause() }
            }
        }
    }

    func stop() {
        sessionQueue.async { [self] in
            if capture.isRunning { capture.stopRunning() }
            device = nil
            lastConfig = nil
        }
        saveWorldMap(thenPause: true)
    }

    func setZoom(_ z: Double) {
        sessionQueue.async { [self] in
            guard let d = device, (try? d.lockForConfiguration()) != nil else { return }
            d.videoZoomFactor = min(max(z, d.minAvailableVideoZoomFactor), d.maxAvailableVideoZoomFactor)
            d.unlockForConfiguration()
        }
    }

    // MARK: AVCapture (runs on pipeline.queue)
    func captureOutput(_ output: AVCaptureOutput, didOutput sb: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let pb = CMSampleBufferGetImageBuffer(sb), let dev = device else { return }
        let (w, h) = (CVPixelBufferGetWidth(pb), CVPixelBufferGetHeight(pb))
        let zoom = Float(dev.videoZoomFactor)
        let hfov = Float(dev.activeFormat.videoFieldOfView) * .pi / 180  // horizontal, sensor-landscape, at zoom 1
        var K: Intrinsics
        if let d = CMGetAttachment(sb, key: kCMSampleBufferAttachmentKey_CameraIntrinsicMatrix, attachmentModeOut: nil) as? Data,
           d.count >= MemoryLayout<simd_float3x3>.size {
            K = Geo.normalizeCaptureIntrinsics(d.withUnsafeBytes { $0.loadUnaligned(as: simd_float3x3.self) },
                                               bufferW: w, bufferH: h)
            // Apple does not document whether the matrix tracks videoZoomFactor. Compare with the nominal FOV
            // focal length: if f is already ~zoom x nominal it includes zoom, otherwise scale by zoom ourselves.
            let nominal = Float(max(w, h)) / 2 / tan(hfov / 2)
            let accountsForZoom = zoom > 1.2 && K.fx / nominal > zoom.squareRoot()
            if !accountsForZoom { K = Intrinsics(fx: K.fx * zoom, fy: K.fy * zoom, cx: K.cx, cy: K.cy) }
            if zoom > 1.2, !loggedZoom { loggedZoom = true; NSLog("intrinsics include zoom: \(accountsForZoom)") }
        } else {  // fallback: nominal FOV, square pixels, centred
            let f = Float(max(w, h)) / 2 / tan(hfov / 2) * zoom
            K = Intrinsics(fx: f, fy: f, cx: Float(w) / 2, cy: Float(h) / 2)
        }
        pipeline.processCapture(pb, t: epoch(fromUptime: CMSampleBufferGetPresentationTimeStamp(sb).seconds), K: K)
    }

    // MARK: ARKit (runs on pipeline.queue)
    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        let now = frame.timestamp
        let faces = frame.anchors.compactMap { $0 as? ARFaceAnchor }
        // Always the best CURRENT face anchor (tracked, newest); never a cached identifier.
        let pickedID = faceBook.pick(faces.map { .init(id: $0.identifier, tracked: $0.isTracked) }, now: now)
        let face = faces.first { $0.identifier == pickedID }
        fpsCount += 1
        if now - fpsStart >= 1 { fps = fpsStart == 0 ? 0 : Double(fpsCount) / (now - fpsStart); fpsStart = now; fpsCount = 0 }
        if worldMode { world.lastFrame = frame; world.couple(frame) }  // freshest frame for locate / tap; object trackers
        var frontHead: WorldHead?
        var src: FaceSrc?
        if worldMode {
            frontHead = face.map { Geo.worldHead(faceT: $0.transform, leftEye: $0.leftEyeTransform, rightEye: $0.rightEyeTransform) }
            let rear = rearFace.fresh(now: now)
            src = faceSel.update(frontTracked: face != nil, rearAvailable: rear != nil)
            if faceSel.wantsRear { rearFace.submit(frame, ipd: Settings.ipd) }
            if src == .rear { frontHead = rear }
        }
        // The face anchor is front-camera based and independent of world tracking quality: it is sent even while world
        // tracking is limited (ar.tracking tells the server world-frame positions may be degraded).
        var diag = ARDiag(tracking: Self.trackingText(frame.camera.trackingState), faces: faces.count, faceTracked: face != nil,
                          faceAge: faceBook.age(now: now), fps: fps, faceSrc: src?.rawValue)
        if worldMode {
            diag.lux = frame.lightEstimate.map { Double($0.ambientIntensity) }
            diag.features = frame.rawFeaturePoints?.points.count
            if src == nil { diag.faceLostReason = ARDiag.lostReason(frontTracked: face != nil, rearNote: rearFace.note) }
        }
        if worldMode { logChange(diag) }
        if statusSet, diag.tracking == "normal" { statusSet = false; onStatus?(nil) }
        guard now - lastAR >= 0.028 else { return }
        lastAR = now
        if worldMode {
            let head = src == nil ? nil : frontHead
            pipeline.processWorld(WorldFrame(pixelBuffer: frame.capturedImage, intrinsics: frame.camera.intrinsics,
                                             cameraTransform: frame.camera.transform,
                                             captureTime: epoch(fromUptime: frame.timestamp), head: head,
                                             anchors: world.anchors(in: frame), errors: [:], ar: diag))
            return
        }
        let lite = face.map { ARFrameLite.Face(transform: $0.transform, left: $0.leftEyeTransform, right: $0.rightEyeTransform) }
        pipeline.processAR(ARFrameLite(pixelBuffer: frame.capturedImage, intrinsics: frame.camera.intrinsics,
                                       cameraTransform: frame.camera.transform,
                                       captureTime: epoch(fromUptime: frame.timestamp), face: lite))
    }

    static func trackingText(_ s: ARCamera.TrackingState) -> String {
        switch s {
        case .normal: "normal"
        case .notAvailable: "notAvailable"
        case .limited(let r):
            switch r {
            case .initializing: "limited:initializing"
            case .excessiveMotion: "limited:excessiveMotion"
            case .insufficientFeatures: "limited:insufficientFeatures"
            case .relocalizing: "limited:relocalizing"
            @unknown default: "limited:unknown"
            }
        }
    }

    /// os_log only on change of (tracking, face tracked, face count), so a stuck face leaves a readable trail.
    private func logChange(_ d: ARDiag) {
        let key = "\(d.tracking)|\(d.faceTracked)|\(d.faces)"
        guard key != lastLogged else { return }
        lastLogged = key
        log.info("ar tracking=\(d.tracking, privacy: .public) faces=\(d.faces, privacy: .public) faceTracked=\(d.faceTracked, privacy: .public) faceAge=\(d.faceAge ?? -1, privacy: .public) fps=\(d.fps, privacy: .public)")
    }

    // MARK: ARSessionObserver (also on pipeline.queue)
    private func setStatus(_ s: String) { statusSet = true; onStatus?(s) }

    func sessionWasInterrupted(_ session: ARSession) {
        log.error("session interrupted"); setStatus("AR interrupted (camera in use or app inactive); will resume")
    }

    func sessionInterruptionEnded(_ session: ARSession) {
        log.info("session interruption ended"); setStatus("AR resuming…")  // cleared when tracking is normal again
    }

    func sessionShouldAttemptRelocalization(_ session: ARSession) -> Bool { true }

    func session(_ session: ARSession, didFailWithError error: Error) {
        let e = error as NSError
        log.error("session failed: \(e.domain, privacy: .public) \(e.code, privacy: .public) \(e.localizedDescription, privacy: .public)")
        setStatus("AR error \(e.code): \(e.localizedDescription)")
        guard e.code != ARError.cameraUnauthorized.rawValue else { return }
        sessionQueue.asyncAfter(deadline: .now() + 1) { [self] in
            guard let c = lastConfig else { return }
            ar.run(c)  // resume in place: no reset, keeps the world map and anchors
        }
    }

    func session(_ session: ARSession, cameraDidChangeTrackingState camera: ARCamera) {
        log.info("camera tracking \(Self.trackingText(camera.trackingState), privacy: .public)")
    }
}

enum PoseMode: String, CaseIterable, Sendable { case world, face, vision }

@MainActor @Observable
final class CameraModel {
    var running = false
    /// Vision mode only: which physical camera. (World = rear + user-face front; Face = front.)
    var front = false { didSet { if oldValue != front, mode == .vision { cameraChanged() } } }
    var mode: PoseMode {
        didSet {
            guard oldValue != mode else { return }
            UserDefaults.standard.set(mode.rawValue, forKey: "poseMode")
            if mode == .vision { cameraChanged() } else if running { apply() }
        }
    }
    var restoreScene: Bool { didSet { UserDefaults.standard.set(restoreScene, forKey: "restoreScene") } }
    let worldSupported = Rig.worldSupported
    var hasSavedScene: Bool { WorldStore.exists }
    /// Preview is mirrored like a selfie only for the front camera.
    var mirrored: Bool { mode == .face || (mode == .vision && front) }
    var lenses: [Lens] = []
    var lens: Lens? { didSet { if oldValue != lens { zoom = 1; if running { apply() } } } }
    var zoom = 1.0 { didSet { rig.setZoom(zoom) } }
    var zoomRange = 1.0...1.0
    var camState = WSState.idle
    var poseState = WSState.idle
    var stats = FrameReport()  // status row; refreshed ~4 Hz
    let overlay = OverlayState()  // face/target overlay; refreshed ~20 Hz, observed only by OverlayView
    var error: String?
    var arStatus: String?  // AR interruption / failure text
    var needsServer = false  // set when Start is refused for lack of a server URL; the view opens Settings
    let arSupported = ARFaceTrackingConfiguration.isSupported
    let previewSink = PreviewSink()

    private var devices: [String: AVCaptureDevice] = [:]
    private let pipeline = Pipeline()
    private let rig: Rig
    var availableModes: [PoseMode] { (worldSupported ? [.world] : []) + (arSupported ? [.face] : []) + [.vision] }

    init() {
        let saved = PoseMode(rawValue: UserDefaults.standard.string(forKey: "poseMode") ?? "")
        let ok = (Rig.worldSupported ? [PoseMode.world] : []) + (ARFaceTrackingConfiguration.isSupported ? [.face] : []) + [.vision]
        mode = saved.flatMap { ok.contains($0) ? $0 : nil } ?? ok[0]
        restoreScene = UserDefaults.standard.bool(forKey: "restoreScene")
        rig = Rig(pipeline: pipeline)
        pipeline.onReport = { [weak self] r in
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.received(r) } }
        }
        rig.onStatus = { [weak self] s in Task { @MainActor in self?.arStatus = s } }
        pipeline.onPreview = { [previewSink] cg in previewSink.show(cg) }
        pipeline.camSocket.onState = { [weak self] s in Task { @MainActor in self?.camState = s } }
        pipeline.poseSocket.onState = { [weak self] s in Task { @MainActor in self?.poseState = s } }
        cameraChanged()
    }

    private var lastStats = 0.0
    private func received(_ r: FrameReport) {
        overlay.report = r
        let now = ProcessInfo.processInfo.systemUptime
        if now - lastStats >= 0.25 { lastStats = now; stats = r }
    }

    /// Settings changed while streaming: restart the sockets (debounced so typing a URL doesn't thrash).
    private var reloadTask: Task<Void, Never>?
    func settingsChanged() {
        guard running else { return }
        reloadTask?.cancel()
        reloadTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(500))
            guard !Task.isCancelled, let self, self.running else { return }
            self.pipeline.reload(server: UserDefaults.standard.string(forKey: Keys.server) ?? "")
        }
    }

    private func cameraChanged() {
        let pos: AVCaptureDevice.Position = front ? .front : .back
        let found = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.builtInUltraWideCamera, .builtInWideAngleCamera, .builtInTelephotoCamera, .builtInTrueDepthCamera],
            mediaType: .video, position: pos).devices
        var seen = Set<String>()
        let uniq = found.filter { seen.insert($0.uniqueID).inserted }
        let wideFOV = uniq.first { $0.deviceType == .builtInWideAngleCamera || $0.deviceType == .builtInTrueDepthCamera }?
            .activeFormat.videoFieldOfView
        devices = Dictionary(uniqueKeysWithValues: uniq.map { ($0.uniqueID, $0) })
        lenses = uniq.map { d in
            var base = 1.0
            if let w = wideFOV, d.activeFormat.videoFieldOfView > 0 {
                let r = tan(Double(w) * .pi / 360) / tan(Double(d.activeFormat.videoFieldOfView) * .pi / 360)
                base = max((r * 2).rounded() / 2, 0.5)
            }
            let name = ["AVCaptureDeviceTypeBuiltInUltraWideCamera": "ultrawide", "AVCaptureDeviceTypeBuiltInTelephotoCamera": "tele"][d.deviceType.rawValue] ?? "wide"
            return Lens(id: d.uniqueID, label: String(format: base == base.rounded() ? "%.0f×" : "%.1f×", base), name: name, base: base)
        }.sorted { $0.base < $1.base }
        lens = lenses.first { $0.base == 1 } ?? lenses.first
        if running { apply() }
    }

    func toggle() { if running { stop() } else { Task { await start() } } }

    func start() async {
        guard Settings.wsURL(UserDefaults.standard.string(forKey: Keys.server) ?? "", path: "/") != nil else {
            needsServer = true; return
        }
        guard await AVCaptureDevice.requestAccess(for: .video) else { error = "Camera access denied (Settings > Bluebook Camera)"; return }
        error = nil; running = true
        UIApplication.shared.isIdleTimerDisabled = true
        apply()
    }

    func stop() {
        running = false
        UIApplication.shared.isIdleTimerDisabled = false
        rig.stop(); pipeline.stop()
        stats = FrameReport(); overlay.report = FrameReport(); arStatus = nil
    }

    func saveScene() { if running && mode == .world { rig.saveWorldMap() } }

    /// Tap on the preview (normalised point of the sent image): place the "tapped" anchor, then `done(ok)`.
    func tapPlace(_ n: CGPoint, done: @escaping @MainActor (Bool) -> Void) {
        guard running, mode == .world else { return }
        let world = rig.world
        pipeline.queue.async {
            let ok = world.locate(id: "tapped", box: CGRect(x: n.x - 0.03, y: n.y - 0.02, width: 0.06, height: 0.04), fraction: 1)
            Task { @MainActor in done(ok) }
        }
    }

    func forget(_ id: String) { let w = rig.world; pipeline.queue.async { w.forget(id) } }

    /// (Re)start sources and sockets for the current camera/lens/mode.
    private func apply() {
        let server = UserDefaults.standard.string(forKey: Keys.server) ?? ""
        var model = "iPhone"
        var u = utsname(); uname(&u)
        withUnsafeBytes(of: &u.machine) { model = String(decoding: $0.prefix { $0 != 0 }, as: UTF8.self) }
        if mode == .world {
            pipeline.configure(source: .world, device: "\(model) rear world+lidar", server: server)
            rig.startWorld(restore: restoreScene)
        } else if mode == .face {
            pipeline.configure(source: .arkit, device: "\(model) front arkit", server: server)
            rig.startAR()
        } else if let l = lens, let d = devices[l.id] {
            pipeline.configure(source: .vision, device: "\(model) \(front ? "front" : "back") \(l.name)", server: server)
            zoomRange = d.minAvailableVideoZoomFactor...min(d.maxAvailableVideoZoomFactor, 15)
            rig.startCapture(d)
        } else { error = "No camera found" }
    }
}

@MainActor @Observable
final class OverlayState { var report = FrameReport() }

/// Receives preview frames from the pipeline thread and puts them on the preview layer.
final class PreviewSink: @unchecked Sendable {
    weak var layer: CALayer?
    func show(_ cg: CGImage) { DispatchQueue.main.async { [weak self] in self?.layer?.contents = cg } }
}
