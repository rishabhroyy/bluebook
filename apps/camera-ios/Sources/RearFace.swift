import ARKit
import Foundation
import Vision
import simd

/// Which sensor produced the head pose in World mode.
enum FaceSrc: String, Sendable { case front, rear }

/// Front-vs-rear head source with hysteresis: front whenever its anchor is tracked, else rear; once on rear, return to
/// front only after the front anchor has been tracked for `holdFrames` consecutive frames.
struct FaceSourceSelector {
    static let holdFrames = 3
    private(set) var current: FaceSrc?
    private(set) var frontStreak = 0

    /// True while the rear Vision pass should run (front not yet solidly back).
    var wantsRear: Bool { frontStreak < Self.holdFrames }

    mutating func update(frontTracked: Bool, rearAvailable: Bool) -> FaceSrc? {
        frontStreak = frontTracked ? frontStreak + 1 : 0
        if current == .rear {
            if frontStreak >= Self.holdFrames { current = .front; return .front }
            if rearAvailable { return .rear }
            return frontTracked ? .front : nil  // rear gone: use front meanwhile, but stay in "rear" mode
        }
        if frontTracked { current = .front; return .front }
        if rearAvailable { current = .rear; return .rear }
        current = nil
        return nil
    }

    mutating func reset() { self = FaceSourceSelector() }
}

extension Geo {
    /// ARKit camera (landscape sensor: x right, y up, z back) <-> OpenCV camera of the upright (.right) image. Self-inverse
    /// (symmetric, det +1), so the same matrix maps the upright camera frame back to the ARKit camera frame.
    ///
    /// Rear-camera derivation: Vision on the landscape buffer with orientation .right works on the upright image, so
    /// Geo.rotation(...) is head->OpenCV-upright-camera (head-on = diag(1,-1,-1)). Then
    /// R_world<-head = R_world<-arcam * A^-1 * R_cv<-head, A^-1 == A. Head-on: head z = +z_ar, toward the phone.
    static func rearWorldRotation(camT: simd_float4x4, rCV: simd_float3x3) -> simd_float3x3 {
        simd_float3x3(camT.columns.0.xyz, camT.columns.1.xyz, camT.columns.2.xyz) * (arkitToUpright * rCV)
    }

    /// Upright pixel (portrait, top-left origin, image `uprightW` wide) -> sensor-landscape pixel. Inverse of (u,v) -> (H-v, u).
    static func landscapePixel(fromUpright p: CGPoint, landscapeH: Float) -> (u: Float, v: Float) {
        (Float(p.y), landscapeH - Float(p.x))
    }

    /// Median depth (metres) of a (2r+1)^2 patch of a landscape depth grid around `centre` (landscape normalised).
    /// Samples must be finite, 0.1...8 m and, when `conf` is given, >= `minConf`. nil with fewer than `minCount` valid.
    static func patchMedianDepth(depth: UnsafePointer<Float32>, stride: Int, conf: UnsafePointer<UInt8>?, confStride: Int,
                                 w: Int, h: Int, centre: CGPoint, radius: Int = 3, minConf: UInt8 = 1, minCount: Int = 5) -> Float? {
        let cx = min(max(Int(centre.x * CGFloat(w)), 0), w - 1), cy = min(max(Int(centre.y * CGFloat(h)), 0), h - 1)
        var v: [Float] = []
        for y in max(cy - radius, 0)...min(cy + radius, h - 1) {
            for x in max(cx - radius, 0)...min(cx + radius, w - 1) {
                let z = depth[y * stride + x]
                guard z.isFinite, z > 0.1, z < 8 else { continue }
                if let conf, conf[y * confStride + x] < minConf { continue }
                v.append(z)
            }
        }
        guard v.count >= minCount else { return nil }
        v.sort()
        return v.count % 2 == 1 ? v[v.count / 2] : (v[v.count / 2 - 1] + v[v.count / 2]) / 2
    }

    /// Head origin in world from a LiDAR depth `d` (z-depth) at landscape pixel (u,v): back-project, then push 1.5 cm
    /// along the camera ray (the sampled surface is the face, ~1-2 cm in front of the eye centres).
    static func rearOrigin(u: Float, v: Float, depth d: Float, K: Intrinsics, camT: simd_float4x4, eyeOffset: Float = 0.015) -> SIMD3<Float> {
        let c = SIMD3<Float>((u - K.cx) / K.fx * d, -(v - K.cy) / K.fy * d, -d)
        return (camT * SIMD4<Float>((c + simd_normalize(c) * eyeOffset), 1)).xyz
    }

    /// IPD-based fallback: `tCV` (OpenCV upright camera frame, from Geo.position) -> world.
    static func rearOriginIPD(tCV: SIMD3<Float>, camT: simd_float4x4) -> SIMD3<Float> {
        (camT * SIMD4<Float>(arkitToUpright * tCV, 1)).xyz
    }
}

/// Vision regions of interest (normalised, bottom-left origin, in the upright image). Vision returns observations in
/// FULL-image coordinates even with a region of interest (checked on macOS with rectangles), so no remapping is needed.
enum RearROI {
    /// Scale `b` about its centre by `f`, clamped to the unit square.
    static func expand(_ b: CGRect, by f: CGFloat) -> CGRect {
        let w = b.width * f, h = b.height * f
        return CGRect(x: b.midX - w / 2, y: b.midY - h / 2, width: w, height: h).intersection(CGRect(x: 0, y: 0, width: 1, height: 1))
    }

    /// Where a head can be inside a person box: the top 60% of it, widened 30%.
    static func head(inBody b: CGRect) -> CGRect {
        expand(CGRect(x: b.minX, y: b.maxY - 0.6 * b.height, width: b.width, height: 0.6 * b.height), by: 1.3)
    }
}

/// Rear-camera head pose for World mode: Apple Vision (rev 3 angles + landmarks) on the ARFrame's rear image, LiDAR for
/// metric position. Runs on its own queue, one job in flight, <= 30 Hz; results are already in the ARKit world frame.
final class RearFace: @unchecked Sendable {
    private struct Job: @unchecked Sendable {
        var pb: CVPixelBuffer; var depth: ARDepthData?; var K: Intrinsics; var camT: simd_float4x4; var t: Double; var ipd: Float
        var seedBox: CGRect?  // recent face box: search only around it
        var missingFor = 0.0  // s without any face
    }
    private let queue = DispatchQueue(label: "bluebook.rearface", qos: .userInitiated)
    private let lock = NSLock()
    private var busy = false, lastStart = 0.0
    private var latest: (head: WorldHead, t: Double)?
    private var lastFace: (box: CGRect, t: Double)?  // Vision box of the last detected face (bottom-left, upright)
    private var missingSince: Double?
    private var lastNote: String?
    private static let recent = 1.0  // s a face box stays a valid ROI seed
    private let bodyReq = VNDetectHumanRectanglesRequest()
    private let minInterval = 1 / 30.0, maxAge = 0.3
    private let poseReq: VNDetectFaceRectanglesRequest = {
        let r = VNDetectFaceRectanglesRequest()
        r.revision = VNDetectFaceRectanglesRequestRevision3  // continuous roll/yaw/pitch (earlier revisions snap in bins)
        return r
    }()
    private let lmReq = VNDetectFaceLandmarksRequest()

    func reset() { lock.withLock { latest = nil; busy = false; lastStart = 0; lastFace = nil; missingSince = nil; lastNote = nil } }

    /// Why the last rear pass found no head (nil: found, or never ran).
    var note: String? { lock.withLock { lastNote } }

    /// Most recent result no older than `maxAge` s (ARFrame.timestamp clock).
    func fresh(now: Double) -> WorldHead? { lock.withLock { latest.flatMap { now - $0.t <= maxAge ? $0.head : nil } } }

    func submit(_ f: ARFrame, ipd: Float) {
        var seed: CGRect?, missing = 0.0
        let go = lock.withLock { () -> Bool in
            if busy || f.timestamp - lastStart < minInterval { return false }
            busy = true; lastStart = f.timestamp
            if let lf = lastFace, f.timestamp - lf.t <= Self.recent { seed = lf.box }
            missing = missingSince.map { f.timestamp - $0 } ?? 0
            return true
        }
        guard go else { return }
        let k = f.camera.intrinsics
        let job = Job(pb: f.capturedImage, depth: f.smoothedSceneDepth ?? f.sceneDepth,
                      K: Intrinsics(fx: k[0][0], fy: k[1][1], cx: k[2][0], cy: k[2][1]), camT: f.camera.transform, t: f.timestamp, ipd: ipd,
                      seedBox: seed, missingFor: missing)
        queue.async { [self] in
            let (h, box, why) = run(job)
            lock.withLock {
                busy = false; lastNote = h == nil ? why : nil
                if let box { lastFace = (box, job.t); missingSince = nil } else if missingSince == nil { missingSince = lastFace?.t ?? job.t }
                if let h { latest = (h, job.t) }
            }
        }
    }

    private func faces(_ pb: CVPixelBuffer, roi: CGRect?) -> VNFaceObservation? {
        poseReq.regionOfInterest = roi ?? CGRect(x: 0, y: 0, width: 1, height: 1)
        guard (try? VNImageRequestHandler(cvPixelBuffer: pb, orientation: .right).perform([poseReq])) != nil else { return nil }
        return (poseReq.results ?? []).max(by: { $0.boundingBox.area < $1.boundingBox.area })
    }

    /// Recall strategy: around the last face when there is a recent one (fast, small search area); otherwise the full image,
    /// and after > 1 s without a face a coarse person detector locates a region to search for the face in.
    private func run(_ j: Job) -> (head: WorldHead?, box: CGRect?, why: String?) {
        let (lw, lh) = (CVPixelBufferGetWidth(j.pb), CVPixelBufferGetHeight(j.pb))
        let size = CGSize(width: lh, height: lw)  // upright (.right) image
        var found: VNFaceObservation?
        if let seed = j.seedBox {
            found = faces(j.pb, roi: RearROI.expand(seed, by: 2))
        } else {
            found = faces(j.pb, roi: nil)
            if found == nil, j.missingFor > Self.recent,
               (try? VNImageRequestHandler(cvPixelBuffer: j.pb, orientation: .right).perform([bodyReq])) != nil,
               let body = (bodyReq.results ?? []).max(by: { $0.boundingBox.area < $1.boundingBox.area }) {
                found = faces(j.pb, roi: RearROI.head(inBody: body.boundingBox))
            }
        }
        guard let poseObs = found else { return (nil, nil, "no face detected") }
        let box = poseObs.boundingBox
        guard let roll = poseObs.roll?.floatValue, let yaw = poseObs.yaw?.floatValue else { return (nil, box, "no head angles") }
        lmReq.inputFaceObservations = [poseObs]
        guard (try? VNImageRequestHandler(cvPixelBuffer: j.pb, orientation: .right).perform([lmReq])) != nil,
              let obs = lmReq.results?.first, let lm = obs.landmarks else { return (nil, box, "no landmarks") }
        func pt(_ r: VNFaceLandmarkRegion2D?) -> CGPoint? {
            guard let r, r.pointCount > 0 else { return nil }
            let p = r.pointsInImage(imageSize: size)
            let c = p.reduce(CGPoint.zero) { CGPoint(x: $0.x + $1.x, y: $0.y + $1.y) }
            return CGPoint(x: c.x / CGFloat(p.count), y: size.height - c.y / CGFloat(p.count))
        }
        guard let l = pt(lm.leftPupil) ?? pt(lm.leftEye), let r = pt(lm.rightPupil) ?? pt(lm.rightEye) else { return (nil, box, "no eyes") }
        let rCV = Geo.rotation(roll: roll, yaw: yaw, pitch: poseObs.pitch?.floatValue ?? 0)
        let conf = lm.confidence > 0 ? lm.confidence : obs.confidence
        let mid = CGPoint(x: (l.x + r.x) / 2, y: (l.y + r.y) / 2)
        let (u, v) = Geo.landscapePixel(fromUpright: mid, landscapeH: Float(lh))
        let R = Geo.rearWorldRotation(camT: j.camT, rCV: rCV)
        if let d = Self.depthAt(j.depth, CGPoint(x: Double(u) / Double(lw), y: Double(v) / Double(lh))) {
            return (WorldHead(R: R, t: Geo.rearOrigin(u: u, v: v, depth: d, K: j.K, camT: j.camT), src: .rear, conf: conf, lidar: true), box, nil)
        }
        // no usable depth: IPD distance in the upright frame
        let Kp = Geo.arkitIntrinsics(simd_float3x3(columns: ([j.K.fx, 0, 0], [0, j.K.fy, 0], [j.K.cx, j.K.cy, 1])), landscapeH: Float(lh))
        guard let tCV = Geo.position(left: l, right: r, K: Kp, yaw: yaw, ipd: j.ipd) else { return (nil, box, "no depth") }
        return (WorldHead(R: R, t: Geo.rearOriginIPD(tCV: tCV, camT: j.camT), src: .rear, conf: conf, lidar: false), box, nil)
    }

    private static func depthAt(_ d: ARDepthData?, _ centre: CGPoint) -> Float? {
        guard let d else { return nil }
        let m = d.depthMap
        CVPixelBufferLockBaseAddress(m, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(m, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(m), CVPixelBufferGetPixelFormatType(m) == kCVPixelFormatType_DepthFloat32 else { return nil }
        var conf: UnsafePointer<UInt8>?, confStride = 0
        let cm = d.confidenceMap
        if let cm { CVPixelBufferLockBaseAddress(cm, .readOnly) }
        defer { if let cm { CVPixelBufferUnlockBaseAddress(cm, .readOnly) } }
        if let cm, let cb = CVPixelBufferGetBaseAddress(cm) {
            conf = UnsafePointer(cb.assumingMemoryBound(to: UInt8.self)); confStride = CVPixelBufferGetBytesPerRow(cm)
        }
        return Geo.patchMedianDepth(depth: UnsafePointer(base.assumingMemoryBound(to: Float32.self)),
                                    stride: CVPixelBufferGetBytesPerRow(m) / MemoryLayout<Float32>.size, conf: conf, confStride: confStride,
                                    w: CVPixelBufferGetWidth(m), h: CVPixelBufferGetHeight(m), centre: centre,
                                    minConf: UInt8(ARConfidenceLevel.medium.rawValue))
    }
}
