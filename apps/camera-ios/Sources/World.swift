import ARKit
import Foundation
import simd

struct WorldAnchor: Sendable {
    var id: String; var p: SIMD3<Float>; var how: String
    var conf: Float = 1  // tracker confidence (1.0 for static / camera anchors)
    var ageS: Double = 0  // seconds since the position was last measured
    var box: CGRect?  // coupled anchors: tracked box, top-left normalised in the upright image (preview only)

    /// Wire form inside the pose message `anchors[]`.
    var json: [String: Any] {
        ["id": id, "p": [Double(p.x), Double(p.y), Double(p.z)], "how": how,
         "conf": (Double(conf) * 100).rounded() / 100, "age_s": (max(ageS, 0) * 100).rounded() / 100]
    }
}

/// What the pipeline needs from one world-tracking ARFrame.
struct WorldFrame: @unchecked Sendable {
    var pixelBuffer: CVPixelBuffer
    var intrinsics: simd_float3x3  // sensor-landscape pixels of pixelBuffer
    var cameraTransform: simd_float4x4
    var captureTime: Double
    var head: WorldHead?
    var anchors: [WorldAnchor]
    var errors: [String: String]
    var ar = ARDiag()
}

/// App-side world anchors (ARAnchor named by target id) and robust 3D placement of boxes / taps.
/// Everything here runs on `Pipeline.queue` (also the ARSession delegate queue), except `howSnapshot`.
final class WorldEngine: @unchecked Sendable {
    private struct Placed {
        var anchor: ARAnchor; var p: SIMD3<Float>; var how: String
        var measuredAt = 0.0  // ARFrame.timestamp clock
        var conf: Float = 1
        var coupling: Coupling?  // non-nil: an object tracker refines `p` (never drags it: see Coupling.refines)
        var movedReportedAt = -Double.infinity  // last "moved" error (rate-limited)
        var box: CGRect?
    }
    private let session: ARSession
    private var known: [String: Placed] = [:]
    private var removed = Set<UUID>()
    private var errors: [String: String] = [:]
    private var meshOn = false
    private var raycastOK = true  // false in front-camera world mode (face configuration: no plane estimation)
    private var frontDepth: (d: DepthSource, t: Double)?  // TrueDepth arrives on ~1 in 4 frames: keep the newest
    private let lock = NSLock()
    private var howMap: [String: String] = [:]  // mirror of id -> how for persistence (any thread)
    private var savedHow: [String: String] = [:]  // loaded sidecar, used when anchors come back from a world map
    var lastFrame: ARFrame?
    private var tracker: ObjectTracker!

    /// `queue`: the pipeline queue; tracker results hop back onto it before touching any state here.
    init(session: ARSession, queue: DispatchQueue) {
        self.session = session
        tracker = ObjectTracker { [weak self] r in queue.async { self?.apply(r) } }
    }

    func reset(meshOn: Bool, savedHow: [String: String], raycast: Bool = true) {
        known = [:]; removed = []; errors = [:]; lastFrame = nil; tracker.stopAll(); frontDepth = nil
        self.meshOn = meshOn; self.savedHow = savedHow; raycastOK = raycast
        lock.withLock { howMap = savedHow }
    }

    func howSnapshot() -> [String: String] { lock.withLock { howMap } }
    func pendingErrors() -> [String: String] { errors }
    func clearErrors(_ keys: [String]) { keys.forEach { errors[$0] = nil } }

    /// Current world position of every placed anchor (ARKit refines these over time), adopting restored ones.
    func anchors(in frame: ARFrame) -> [WorldAnchor] {
        var live: [UUID: SIMD3<Float>] = [:]
        for a in frame.anchors where type(of: a) == ARAnchor.self {
            guard let n = a.name, !removed.contains(a.identifier) else { continue }
            live[a.identifier] = a.transform.columns.3.xyz
            if known[n] == nil {
                let how = savedHow[n] ?? "estimate"
                known[n] = Placed(anchor: a, p: a.transform.columns.3.xyz, how: how, measuredAt: frame.timestamp)
                lock.withLock { howMap[n] = how }
            }
        }
        return known.keys.sorted().map { id in
            let pl = known[id]!
            let p = pl.coupling != nil ? pl.p : (live[pl.anchor.identifier] ?? pl.p)  // coupled: tracker owns the position
            known[id]!.p = p
            return WorldAnchor(id: id, p: p, how: pl.how, conf: pl.conf, ageS: frame.timestamp - pl.measuredAt, box: pl.box)
        }
    }

    func place(_ id: String, _ p: SIMD3<Float>, how: String) {
        if let old = known[id] { removed.insert(old.anchor.identifier); session.remove(anchor: old.anchor) }
        var t = matrix_identity_float4x4
        t.columns.3 = SIMD4<Float>(p.x, p.y, p.z, 1)
        let a = ARAnchor(name: id, transform: t)
        session.add(anchor: a)
        known[id] = Placed(anchor: a, p: p, how: how, measuredAt: lastFrame?.timestamp ?? 0)
        tracker.stop(id)
        errors[id] = nil
        lock.withLock { howMap[id] = how }
    }

    func forget(_ id: String) {
        if let old = known.removeValue(forKey: id) { removed.insert(old.anchor.identifier); session.remove(anchor: old.anchor) }
        tracker.stop(id)
        errors[id] = nil
        lock.withLock { howMap[id] = nil }
    }

    /// 5x5 grid inside the central `fraction` of a normalised box (upright sent image), 3D-placed and robustly centred.
    /// Returns false (and records errors[id] = "no depth") when nothing could be placed.
    @discardableResult
    func locate(id: String, box: CGRect, fraction: CGFloat = 0.5) -> Bool {
        guard let f = lastFrame else { fail(id); return false }
        let pts = Self.gridPoints(box: box, fraction: fraction)  // sensor-landscape normalised
        let d = depth(f)
        var hits = depthHits(f, d, pts)
        var how = d?.K == nil ? "lidar" : "truedepth"
        if hits.count < 5, raycastOK {
            let r = raycastHits(f, pts)
            if r.points.count > hits.count { hits = r.points; how = r.how }
        }
        guard let c = Geo.robustCentre(hits) else { fail(id); return false }
        place(id, c, how: how)
        // Couple the anchor to the object: a Vision tracker keeps re-measuring it (see `apply`).
        tracker.start(id, box: box)
        known[id]?.coupling = Coupling()
        return true
    }

    /// Feed every world frame; the tracker picks its own rate (<= 15 Hz) and skips while busy.
    func couple(_ f: ARFrame) {
        guard !tracker.isEmpty else { return }
        let k = f.camera.intrinsics
        tracker.submit(pb: f.capturedImage, depth: depth(f),
                       K: Intrinsics(fx: k[0][0], fy: k[1][1], cx: k[2][0], cy: k[2][1]), camT: f.camera.transform, t: f.timestamp)
    }

    /// Tracker result (on the pipeline queue): move a coupled anchor, or hold it and flag "lost" once.
    func apply(_ r: ObjectTracker.Result) {
        guard var pl = known[r.id], var c = pl.coupling else { return }
        let o = c.update(conf: r.conf, inImage: r.inImage, now: r.t)
        pl.coupling = c; pl.box = r.box; pl.conf = r.conf; pl.how = o.state.rawValue
        if o.measure, let p = r.p {
            if Coupling.refines(p, pl.p) {
                pl.p = p; pl.measuredAt = r.t
            } else if r.t - pl.movedReportedAt > 3 {  // disagrees: keep the world point, ask for a re-locate
                pl.movedReportedAt = r.t; errors[r.id] = "moved"
            }
        }
        if o.lost { errors[r.id] = "lost" }
        known[r.id] = pl
    }

    private func fail(_ id: String) {
        forget(id); errors[id] = "no depth"
    }

    /// 5x5 grid inside the central `fraction` of a top-left normalised portrait box, as sensor-landscape normalised points.
    static func gridPoints(box: CGRect, fraction: CGFloat) -> [CGPoint] {
        let w = box.width * fraction, h = box.height * fraction
        let x0 = box.midX - w / 2, y0 = box.midY - h / 2
        var pts: [CGPoint] = []
        for i in 0..<5 { for j in 0..<5 {
            let p = CGPoint(x: x0 + w * CGFloat(i) / 4, y: y0 + h * CGFloat(j) / 4)
            pts.append(Geo.landscapeNorm(fromPortrait: CGPoint(x: min(max(p.x, 0), 1), y: min(max(p.y, 0), 1))))
        } }
        return pts
    }

    /// The frame's depth: rear LiDAR, else the newest front TrueDepth map (<= 0.25 s old).
    func depth(_ f: ARFrame) -> DepthSource? {
        if let d = f.smoothedSceneDepth ?? f.sceneDepth { return DepthSource(map: d.depthMap, conf: d.confidenceMap, K: nil) }
        if let a = f.capturedDepthData, let d = DepthSource(a) { frontDepth = (d, f.capturedDepthDataTimestamp) }
        guard let fd = frontDepth, f.timestamp - fd.t < 0.25 else { return nil }
        return fd.d
    }

    private func depthHits(_ f: ARFrame, _ d: DepthSource?, _ pts: [CGPoint]) -> [SIMD3<Float>] {
        let k = f.camera.intrinsics
        return Self.depthHits(depth: d, K: Intrinsics(fx: k[0][0], fy: k[1][1], cx: k[2][0], cy: k[2][1]),
                              imageW: Float(CVPixelBufferGetWidth(f.capturedImage)), imageH: Float(CVPixelBufferGetHeight(f.capturedImage)),
                              camT: f.camera.transform, pts: pts)
    }

    /// Back-projected depth hits (LiDAR confidence >= medium, 0.1...8 m) at sensor-landscape normalised `pts`.
    static func depthHits(depth: DepthSource?, K: Intrinsics, imageW iw: Float, imageH ih: Float, camT: simd_float4x4, pts: [CGPoint]) -> [SIMD3<Float>] {
        guard let d = depth else { return [] }
        let m = d.map
        CVPixelBufferLockBaseAddress(m, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(m, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(m), CVPixelBufferGetPixelFormatType(m) == kCVPixelFormatType_DepthFloat32 else { return [] }
        let (w, h) = (CVPixelBufferGetWidth(m), CVPixelBufferGetHeight(m))
        let stride = CVPixelBufferGetBytesPerRow(m) / MemoryLayout<Float32>.size
        let depth = base.assumingMemoryBound(to: Float32.self)
        var conf: UnsafePointer<UInt8>?, confStride = 0
        let cm = d.conf
        if let cm { CVPixelBufferLockBaseAddress(cm, .readOnly) }
        defer { if let cm { CVPixelBufferUnlockBaseAddress(cm, .readOnly) } }
        if let cm, let cb = CVPixelBufferGetBaseAddress(cm) {
            conf = UnsafePointer(cb.assumingMemoryBound(to: UInt8.self)); confStride = CVPixelBufferGetBytesPerRow(cm)
        }
        var out: [SIMD3<Float>] = []
        for p in pts {
            let (xf, yf) = d.pixel(p, K: K, imageW: iw, imageH: ih, w: w, h: h)
            let x = min(max(Int(xf), 0), w - 1), y = min(max(Int(yf), 0), h - 1)
            let z = depth[y * stride + x]
            guard z.isFinite, z > 0.1, z < 8 else { continue }
            if let conf, conf[y * confStride + x] < UInt8(ARConfidenceLevel.medium.rawValue) { continue }
            out.append(Geo.unprojectARKit(u: Float(p.x) * iw, v: Float(p.y) * ih, depth: z, K: K, camT: camT))
        }
        return out
    }

    private func raycastHits(_ f: ARFrame, _ pts: [CGPoint]) -> (points: [SIMD3<Float>], how: String) {
        var out: [SIMD3<Float>] = [], plane = 0
        for p in pts {
            let q = f.raycastQuery(from: p, allowing: .estimatedPlane, alignment: .any)
            guard let r = session.raycast(q).first else { continue }
            out.append(r.worldTransform.columns.3.xyz)
            switch r.target { case .existingPlaneGeometry, .existingPlaneInfinite: plane += 1; default: break }
        }
        return (out, plane * 2 > out.count ? "plane" : (meshOn ? "mesh" : "estimate"))
    }
}

/// A depth map to place points with: rear LiDAR `sceneDepth` (pixel-aligned with the image) or the front TrueDepth
/// `capturedDepthData` (own resolution/aspect, registered to the front colour camera: sampled through its intrinsics).
struct DepthSource: @unchecked Sendable {
    var map: CVPixelBuffer  // DepthFloat32, metres along the optical axis, sensor-landscape
    var conf: CVPixelBuffer?  // LiDAR only
    var K: Intrinsics?  // depth-map pixels; nil = aligned with the image, sample by normalised position

    init(map: CVPixelBuffer, conf: CVPixelBuffer?, K: Intrinsics?) { self.map = map; self.conf = conf; self.K = K }

    init?(_ a: AVDepthData) {
        let d = a.depthDataType == kCVPixelFormatType_DepthFloat32 ? a : a.converting(toDepthDataType: kCVPixelFormatType_DepthFloat32)
        guard let cal = d.cameraCalibrationData, cal.intrinsicMatrixReferenceDimensions.width > 0 else { return nil }
        let m = d.depthDataMap
        let s = Float(CVPixelBufferGetWidth(m)) / Float(cal.intrinsicMatrixReferenceDimensions.width)
        let k = cal.intrinsicMatrix
        // ponytail: assumes the depth map is in the same sensor-landscape orientation as capturedImage (unverified on hw)
        self.init(map: m, conf: nil, K: Intrinsics(fx: k[0][0] * s, fy: k[1][1] * s, cx: k[2][0] * s, cy: k[2][1] * s))
    }

    /// Depth-map pixel for a sensor-landscape normalised image point (image intrinsics K, image size iw x ih).
    func pixel(_ p: CGPoint, K img: Intrinsics, imageW iw: Float, imageH ih: Float, w: Int, h: Int) -> (Float, Float) {
        guard let k = K else { return (Float(p.x) * Float(w), Float(p.y) * Float(h)) }
        let x = (Float(p.x) * iw - img.cx) / img.fx, y = (Float(p.y) * ih - img.cy) / img.fy  // ray, same camera centre
        return (k.fx * x + k.cx, k.fy * y + k.cy)
    }
}

/// Saved ARWorldMap (+ how-sidecar) in Documents, so placed objects survive relaunch.
enum WorldStore {
    static var dir: URL { FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0] }
    static var mapURL: URL { dir.appendingPathComponent("world.map") }
    static var howURL: URL { dir.appendingPathComponent("world-anchors.json") }
    static var exists: Bool { FileManager.default.fileExists(atPath: mapURL.path) }

    static func save(_ map: ARWorldMap, how: [String: String]) {
        guard let d = try? NSKeyedArchiver.archivedData(withRootObject: map, requiringSecureCoding: true) else { return }
        try? d.write(to: mapURL, options: .atomic)
        try? JSONSerialization.data(withJSONObject: how).write(to: howURL, options: .atomic)
    }

    static func load() -> (map: ARWorldMap, how: [String: String])? {
        guard let d = try? Data(contentsOf: mapURL),
              let m = try? NSKeyedUnarchiver.unarchivedObject(ofClass: ARWorldMap.self, from: d) else { return nil }
        let how = (try? Data(contentsOf: howURL)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] } ?? [:]
        return (m, how)
    }
}
