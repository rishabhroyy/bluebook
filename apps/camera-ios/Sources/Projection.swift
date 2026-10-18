import CoreGraphics
import SwiftUI
import simd

/// World -> rear-camera preview projection (ARKit world, portrait upright image). Same math as the anchors' `pt`:
/// Geo.projectARKit with the sensor-landscape intrinsics, then Geo.portraitNorm. Values are NOT clipped to the image.
struct WorldProjector {
    var K: Intrinsics  // sensor-landscape pixels
    var landscape: CGSize  // sensor-landscape image size (w, h)
    var camT: simd_float4x4

    static let nearZ: Float = 0.05

    init(K: Intrinsics, landscape: CGSize, camT: simd_float4x4) { self.K = K; self.landscape = landscape; self.camT = camT }
    init?(_ s: SceneSnapshot) {
        guard let K = s.K, s.landscape.width > 0 else { return nil }
        self.init(K: K, landscape: s.landscape, camT: s.camT)
    }

    /// Point -> (portrait-normalised position, depth along the optical axis); nil if behind the camera.
    func project(_ w: SIMD3<Float>) -> (n: CGPoint, depth: Float)? {
        guard let q = Geo.projectARKit(w, K: K, camT: camT) else { return nil }
        return (Geo.portraitNorm(fromLandscape: CGPoint(x: Double(q.u) / landscape.width, y: Double(q.v) / landscape.height)), q.depth)
    }

    /// Depth along the optical axis (negative = behind).
    func depth(_ w: SIMD3<Float>) -> Float { -(camT.inverse * SIMD4<Float>(w.x, w.y, w.z, 1)).z }

    /// Segment a-b clipped to the near plane, as portrait-normalised endpoints (nil if wholly behind).
    func segment(_ a: SIMD3<Float>, _ b: SIMD3<Float>) -> (CGPoint, CGPoint)? {
        let (da, db) = (depth(a), depth(b))
        if da < Self.nearZ && db < Self.nearZ { return nil }
        func at(_ d: Float) -> SIMD3<Float> { a + (b - a) * ((Self.nearZ + 0.001 - da) / (db - da)) }
        let (pa, pb) = (da < Self.nearZ ? at(da) : a, db < Self.nearZ ? at(db) : b)
        guard let qa = project(pa), let qb = project(pb) else { return nil }
        return (qa.n, qb.n)
    }

    /// On-screen radius, in portrait image pixels, of a world sphere of `radius` metres at `depth`.
    func radiusPx(_ radius: Float, depth: Float) -> CGFloat { CGFloat(K.fx * radius / max(depth, Self.nearZ)) }

    /// Unit direction in portrait screen space (y down) toward a world point. Landscape offset (x, -y) maps to portrait (y, x).
    func screenDirection(_ w: SIMD3<Float>) -> CGPoint {
        let c = (camT.inverse * SIMD4<Float>(w.x, w.y, w.z, 1)).xyz
        let d = CGPoint(x: Double(c.y), y: Double(c.x)), l = hypot(d.x, d.y)
        return l < 1e-6 ? CGPoint(x: 0, y: -1) : CGPoint(x: d.x / l, y: d.y / l)
    }
}

/// The head drawn on the rear feed: eye midpoint, three axes (15 cm) and a gaze ray (40 cm along +z).
struct HeadProjection {
    var eye: CGPoint  // portrait-normalised
    var axes: [(CGPoint, CGPoint)?]  // x, y, z segments from the eye
    var gaze: (CGPoint, CGPoint)?
    var depth: Float

    static let axisLen: Float = 0.15, gazeLen: Float = 0.4

    /// nil when the eye midpoint is behind the camera or outside the image (front-camera case).
    static func make(_ h: WorldHead, _ p: WorldProjector) -> HeadProjection? {
        guard let e = p.project(h.t), (0...1).contains(e.n.x), (0...1).contains(e.n.y) else { return nil }
        return HeadProjection(eye: e.n, axes: (0..<3).map { p.segment(h.t, h.t + h.R[$0] * axisLen) },
                              gaze: p.segment(h.t, h.t + h.R[2] * gazeLen), depth: e.depth)
    }

    /// "head: front camera · 52 cm · 12° LEFT 5° UP" (position of the phone seen from the head, like the schematic).
    static func badge(_ h: WorldHead, phone: SIMD3<Float>) -> String {
        let d = Schematic.PhoneInHead(head: h, phone: phone), deg = 180 / Float.pi
        return String(format: "head: %@ camera · %.0f cm · %.0f° %@ %.0f° %@", Schematic.srcLabel(h), d.dist * 100,
                      abs(d.azimuth * deg), d.azimuth >= 0 ? "LEFT" : "RIGHT", abs(d.elevation * deg), d.elevation >= 0 ? "UP" : "DOWN")
    }
}

enum ObjectStyle {
    static let radius: Float = 0.05, axisLen: Float = 0.05
    static func color(how: String) -> Color { how == "tracked" ? .cyan : how == "held" ? .orange : .white }
    static func label(id: String, distM: Float, how: String, conf: Float) -> String {
        let name = id.split(separator: "/").last.map(String.init) ?? id
        let coupled = how == "tracked" || how == "held"
        return String(format: "%@ · %.0f cm · %@", name, distM * 100, how) + (coupled ? String(format: " · %.2f", conf) : "")
    }
}
