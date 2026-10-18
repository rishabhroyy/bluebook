import ARKit
import Foundation
import Vision
import simd

/// Box conversions between the wire / UI convention (normalised, TOP-left origin, in the upright sent JPEG) and Vision's
/// (normalised, BOTTOM-left origin, in the image as oriented by the handler). The tracker is always fed the rear
/// `capturedImage` with orientation `.right`, i.e. exactly the upright image that is JPEG-encoded, so only a y flip is needed.
enum TrackGeo {
    static func visionBox(fromTopLeft b: CGRect) -> CGRect { CGRect(x: b.minX, y: 1 - b.maxY, width: b.width, height: b.height) }
    static func topLeftBox(fromVision b: CGRect) -> CGRect { CGRect(x: b.minX, y: 1 - b.maxY, width: b.width, height: b.height) }
    static func clampedUnit(_ b: CGRect) -> CGRect { b.intersection(CGRect(x: 0, y: 0, width: 1, height: 1)) }
    /// "Box has left the image" test: its centre is outside the frame.
    static func centreInside(_ b: CGRect) -> Bool { (0...1).contains(b.midX) && (0...1).contains(b.midY) }
}

/// Coupling state machine for one anchor: tracked -> held once confidence has been below `minConf` (or the box off-image)
/// for more than `grace` s (reported as `lost` exactly once); back to tracked as soon as the tracker recovers.
struct Coupling {
    enum State: String, Sendable { case tracked, held }
    static let minConf: Float = 0.5
    static let grace = 0.5
    struct Out: Equatable { var state: State; var measure: Bool; var lost: Bool }

    /// A placed object is a fixed world point; the tracker may only refine it by this much (LiDAR + box jitter on the
    /// object's surface). A measurement further away is the tracker on something else (re-acquired the background, or its
    /// box stuck in the image while the phone moved, which drags the point along with the camera): ignored, and reported
    /// so the server re-locates the object from a fresh frame.
    static let refineM: Float = 0.08
    static func refines(_ new: SIMD3<Float>, _ current: SIMD3<Float>) -> Bool { simd_distance(new, current) <= refineM }

    private(set) var state = State.tracked
    private var badSince: Double?
    private var reported = false

    mutating func update(conf: Float, inImage: Bool, now: Double) -> Out {
        if conf >= Self.minConf && inImage {
            badSince = nil; reported = false; state = .tracked
            return Out(state: .tracked, measure: true, lost: false)
        }
        if badSince == nil { badSince = now }
        var lost = false
        if now - badSince! > Self.grace {
            state = .held
            if !reported { reported = true; lost = true }
        }
        return Out(state: state, measure: false, lost: lost)
    }
}

/// One Vision object tracker per anchor id, on its own serial queue, <= 15 Hz overall per id, skipped while busy.
/// Each good result re-samples LiDAR in the tracked box. Never blocks the ARKit delegate.
final class ObjectTracker: @unchecked Sendable {
    struct Result: Sendable {
        var id: String; var box: CGRect  // top-left normalised
        var conf: Float; var inImage: Bool; var p: SIMD3<Float>?; var t: Double
    }
    private final class Item {
        let handler = VNSequenceRequestHandler()
        var obs: VNDetectedObjectObservation
        var busy = false, lastStart = -1.0
        init(_ o: VNDetectedObjectObservation) { obs = o }
    }
    private let queue = DispatchQueue(label: "bluebook.objtrack", qos: .userInitiated)
    private let lock = NSLock()
    private var items: [String: Item] = [:]
    private let minInterval = 1 / 15.0
    private let onResult: @Sendable (Result) -> Void

    init(onResult: @escaping @Sendable (Result) -> Void) { self.onResult = onResult }

    var isEmpty: Bool { lock.withLock { items.isEmpty } }

    /// `box`: top-left normalised in the upright image. Replaces any tracker with the same id.
    func start(_ id: String, box: CGRect) {
        let b = TrackGeo.visionBox(fromTopLeft: TrackGeo.clampedUnit(box))
        let o = VNDetectedObjectObservation(boundingBox: CGRect(x: b.minX, y: b.minY, width: max(b.width, 0.005), height: max(b.height, 0.005)))
        lock.withLock { items[id] = Item(o) }
    }

    func stop(_ id: String) { lock.withLock { items[id] = nil } }
    func stopAll() { lock.withLock { items = [:] } }

    func submit(pb: CVPixelBuffer, depth: ARDepthData?, K: Intrinsics, camT: simd_float4x4, t: Double) {
        let due: [(String, Item, VNDetectedObjectObservation)] = lock.withLock {
            items.compactMap { id, it in
                guard !it.busy, t - it.lastStart >= minInterval else { return nil }
                it.busy = true; it.lastStart = t
                return (id, it, it.obs)
            }
        }
        guard !due.isEmpty else { return }
        let buf = PixelBuffer(pb), d = DepthBox(depth)
        queue.async { [self] in
            for (id, it, obs) in due {
                let req = VNTrackObjectRequest(detectedObjectObservation: obs)
                req.trackingLevel = .accurate
                var res = Result(id: id, box: TrackGeo.topLeftBox(fromVision: obs.boundingBox), conf: 0, inImage: true, p: nil, t: t)
                if (try? it.handler.perform([req], on: buf.pb, orientation: .right)) != nil,
                   let r = req.results?.first as? VNDetectedObjectObservation {
                    res.box = TrackGeo.topLeftBox(fromVision: r.boundingBox); res.conf = r.confidence
                    res.inImage = TrackGeo.centreInside(res.box)
                    if r.confidence >= Coupling.minConf && res.inImage {
                        let pts = WorldEngine.gridPoints(box: res.box, fraction: 0.5)
                        let hits = WorldEngine.depthHits(depth: d.depth, K: K, imageW: Float(CVPixelBufferGetWidth(buf.pb)),
                                                         imageH: Float(CVPixelBufferGetHeight(buf.pb)), camT: camT, pts: pts)
                        if hits.count >= 5 { res.p = Geo.robustCentre(hits) }
                    }
                    lock.withLock { if items[id] === it { it.obs = r } }
                }
                let still = lock.withLock { () -> Bool in
                    it.busy = false
                    return items[id] === it
                }
                if still { onResult(res) }
            }
        }
    }
}

struct DepthBox: @unchecked Sendable { let depth: ARDepthData?; init(_ d: ARDepthData?) { depth = d } }
