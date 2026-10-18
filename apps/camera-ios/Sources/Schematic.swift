import SwiftUI
import simd

/// Everything the schematic draws, taken from the same data as the world pose message.
struct SceneSnapshot: Sendable {
    var head: WorldHead?
    var camT: simd_float4x4
    var anchors: [WorldAnchor]
    var K: Intrinsics?  // sensor-landscape intrinsics (preview projection)
    var landscape = CGSize.zero  // sensor-landscape image size
}

/// Pure geometry for the 3D schematic (testable). World = ARKit: x right, y up, z toward the user (rear camera looks -z).
enum Schematic {
    enum View: CaseIterable {
        case top, side
        /// World point -> (u right, v up) on the card. TOP looks down from +y: u = x, v = -z (the work area, -z, is up).
        /// SIDE looks from +x toward -x: u = -z (work area to the right), v = y.
        func uv(_ p: SIMD3<Float>) -> SIMD2<Float> { self == .top ? [p.x, -p.z] : [-p.z, p.y] }
    }

    static func headColor(_ h: WorldHead) -> Color { h.src == .rear ? .pink : .yellow }
    static func srcLabel(_ h: WorldHead) -> String { h.src == .rear ? "rear" : "front" }

    static let axisLen: Float = 0.15
    static let wedgeLen: Float = 0.5
    static let halfH: Float = 10.7 * .pi / 180, halfV: Float = 5.4 * .pi / 180

    /// Uniform scale + centring that fits `pts` (u,v metres) in `size` with a margin; never zooms past `minSpan` metres.
    struct Fit {
        var scale: CGFloat, centre: SIMD2<Float>, size: CGSize
        init(_ pts: [SIMD2<Float>], size: CGSize, margin: CGFloat = 18, minSpan: Float = 0.4) {
            let lo = pts.reduce(SIMD2<Float>(repeating: .infinity), min), hi = pts.reduce(SIMD2<Float>(repeating: -.infinity), max)
            let span = pts.isEmpty ? SIMD2<Float>(repeating: minSpan) : max(hi - lo, SIMD2<Float>(repeating: minSpan))
            centre = pts.isEmpty ? .zero : (lo + hi) / 2
            scale = min((size.width - 2 * margin) / CGFloat(span.x), (size.height - 2 * margin) / CGFloat(span.y))
            self.size = size
        }
        func map(_ uv: SIMD2<Float>) -> CGPoint {
            CGPoint(x: size.width / 2 + CGFloat(uv.x - centre.x) * scale, y: size.height / 2 - CGFloat(uv.y - centre.y) * scale)
        }
    }

    /// Phone body (portrait, ARKit camera axes: x/y span the screen, z through the thickness) corners in world space.
    static func phoneCorners(_ camT: simd_float4x4) -> [SIMD3<Float>] {
        var out: [SIMD3<Float>] = []
        for sx: Float in [-0.075, 0.075] { for sy: Float in [-0.036, 0.036] { for sz: Float in [-0.004, 0.004] {
            out.append((camT * SIMD4<Float>(sx, sy, sz, 1)).xyz)
        } } }
        return out
    }

    static func lookDir(_ camT: simd_float4x4) -> SIMD3<Float> { -camT.columns.2.xyz }
    static func frontCamera(_ camT: simd_float4x4) -> SIMD3<Float> { (camT * SIMD4<Float>(0, 0, 0.005, 1)).xyz }

    /// Head frame axes tips and the four G2 display-window corner rays, in world space.
    static func headTips(_ h: WorldHead) -> [SIMD3<Float>] { (0..<3).map { h.t + h.R[$0] * axisLen } }
    static func wedge(_ h: WorldHead) -> [SIMD3<Float>] {
        [(1, 1), (1, -1), (-1, 1), (-1, -1)].map { sx, sy in
            h.t + h.R * SIMD3<Float>(Float(sx) * tan(halfH), Float(sy) * tan(halfV), 1) * wedgeLen
        }
    }

    /// Points the view must contain.
    static func fitPoints(_ s: SceneSnapshot, _ v: View) -> [SIMD2<Float>] {
        var p = phoneCorners(s.camT) + [lookDir(s.camT) * 0.12 + s.camT.columns.3.xyz] + s.anchors.map(\.p)
        if let h = s.head { p += [h.t] + headTips(h) }
        return p.map(v.uv)
    }

    /// Convex hull (monotone chain) of screen points, for the phone body and the display wedge.
    static func hull(_ pts: [CGPoint]) -> [CGPoint] {
        let p = pts.sorted { ($0.x, $0.y) < ($1.x, $1.y) }
        guard p.count > 2 else { return p }
        func cross(_ o: CGPoint, _ a: CGPoint, _ b: CGPoint) -> CGFloat { (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x) }
        var h: [CGPoint] = []
        for q in p { while h.count >= 2 && cross(h[h.count - 2], h[h.count - 1], q) <= 0 { h.removeLast() }; h.append(q) }
        let lower = h.count + 1
        for q in p.reversed().dropFirst() { while h.count >= lower && cross(h[h.count - 2], h[h.count - 1], q) <= 0 { h.removeLast() }; h.append(q) }
        h.removeLast()
        return h
    }

    /// Phone position in the HEAD frame: distance, azimuth (+ = face's left), elevation (+ = up).
    struct PhoneInHead: Equatable {
        var dist: Float, azimuth: Float, elevation: Float
        init(head: WorldHead, phone: SIMD3<Float>) {
            let d = head.R.transpose * (phone - head.t)
            dist = simd_length(d)
            azimuth = atan2(d.x, d.z)
            elevation = atan2(d.y, (d.x * d.x + d.z * d.z).squareRoot())
        }
        var text: String {
            let deg = 180 / Float.pi
            return String(format: "%.2f m  %.0f° %@  %.0f° %@", dist, abs(azimuth * deg), azimuth >= 0 ? "LEFT" : "RIGHT",
                          abs(elevation * deg), elevation >= 0 ? "UP" : "DOWN")
        }
    }
}

/// Two orthographic views (TOP, SIDE) of phone, head, its axes, anchors and the glasses display window.
struct SchematicCard: View {
    let state: OverlayState

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            TimelineView(.periodic(from: .now, by: 0.1)) { _ in
                let scene = state.report.scene
                HStack(spacing: 8) {
                    ForEach(Array(Schematic.View.allCases.enumerated()), id: \.offset) { _, v in
                        Canvas { ctx, size in draw(ctx, size, v, scene) }
                            .background(.black.opacity(0.35), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                            .frame(height: 150)
                    }
                }
            }
            readout
        }
        .padding(12)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
    }

    @ViewBuilder private var readout: some View {
        if let s = state.report.scene, let h = s.head {
            Label("phone: " + Schematic.PhoneInHead(head: h, phone: s.camT.columns.3.xyz).text + "  [\(Schematic.srcLabel(h)) face]", systemImage: "location.north.line")
                .font(.footnote.weight(.medium).monospacedDigit()).foregroundStyle(.secondary)
        } else {
            Label(state.report.scene == nil ? "waiting for frames" : "no head — phone not facing you?", systemImage: "questionmark.circle")
                .font(.footnote).foregroundStyle(.orange)
        }
    }

    private func draw(_ ctx: GraphicsContext, _ size: CGSize, _ v: Schematic.View, _ s: SceneSnapshot?) {
        let title = v == .top ? "TOP  x→  z↓" : "SIDE  z←  y↑"
        ctx.draw(Text(title).font(.caption2.bold()).foregroundStyle(.secondary), at: CGPoint(x: 8, y: 8), anchor: .topLeading)
        guard let s else { return }
        let fit = Schematic.Fit(Schematic.fitPoints(s, v), size: size)
        func pt(_ p: SIMD3<Float>) -> CGPoint { fit.map(v.uv(p)) }
        func line(_ a: CGPoint, _ b: CGPoint, _ c: Color, _ w: CGFloat = 2, dash: [CGFloat] = []) {
            var p = Path(); p.move(to: a); p.addLine(to: b)
            ctx.stroke(p, with: .color(c), style: .init(lineWidth: w, lineCap: .round, dash: dash))
        }
        func dot(_ p: CGPoint, _ r: CGFloat, _ c: Color) { ctx.fill(Path(ellipseIn: CGRect(x: p.x - r, y: p.y - r, width: 2 * r, height: 2 * r)), with: .color(c)) }

        let cam = s.camT.columns.3.xyz
        // 10 cm scale bar
        let bar = CGFloat(0.1) * fit.scale, y0 = size.height - 10
        line(CGPoint(x: 8, y: y0), CGPoint(x: 8 + bar, y: y0), .white.opacity(0.8), 2)
        ctx.draw(Text("10 cm").font(.system(size: 9)).foregroundStyle(.secondary), at: CGPoint(x: 8 + bar + 4, y: y0), anchor: .leading)

        // glasses display window (wedge), then dashed links
        if let h = s.head {
            let w = Schematic.hull(([h.t] + Schematic.wedge(h)).map(pt))
            if w.count > 2 { var p = Path(); p.addLines(w); p.closeSubpath(); ctx.fill(p, with: .color(Schematic.headColor(h).opacity(0.18))); ctx.stroke(p, with: .color(Schematic.headColor(h).opacity(0.6)), lineWidth: 0.8) }
            line(pt(h.t), pt(cam), .white.opacity(0.6), 1, dash: [4, 3])
            for a in s.anchors { line(pt(h.t), pt(a.p), .mint.opacity(0.5), 1, dash: [3, 3]) }
        }
        // phone body, look arrow (-z), front camera
        let body = Schematic.hull(Schematic.phoneCorners(s.camT).map(pt))
        if body.count > 2 { var p = Path(); p.addLines(body); p.closeSubpath(); ctx.fill(p, with: .color(.white.opacity(0.25))); ctx.stroke(p, with: .color(.white), lineWidth: 1.2) }
        let tip = pt(cam + Schematic.lookDir(s.camT) * 0.12)
        line(pt(cam), tip, .orange, 2)
        dot(tip, 3, .orange)
        dot(pt(Schematic.frontCamera(s.camT)), 2.5, .cyan)
        ctx.draw(Text("phone").font(.system(size: 9, weight: .bold)).foregroundStyle(.white), at: CGPoint(x: pt(cam).x + 6, y: pt(cam).y - 8), anchor: .leading)
        for a in s.anchors {
            let p = pt(a.p), c: Color = a.id == "tapped" ? .green : .mint
            dot(p, 4, c)
            ctx.draw(Text(a.id.split(separator: "/").last.map(String.init) ?? a.id).font(.system(size: 9, weight: .bold)).foregroundStyle(c), at: CGPoint(x: p.x + 6, y: p.y), anchor: .leading)
        }
        // head + axes (x red = face's left, y green = up, z blue = out of the face)
        if let h = s.head {
            for (i, c) in [Color.red, .green, .blue].enumerated() { line(pt(h.t), pt(Schematic.headTips(h)[i]), c, 2.5) }
            dot(pt(h.t), 5, Schematic.headColor(h))
            ctx.draw(Text("head · \(Schematic.srcLabel(h))").font(.system(size: 9, weight: .bold)).foregroundStyle(Schematic.headColor(h)), at: CGPoint(x: pt(h.t).x + 7, y: pt(h.t).y - 8), anchor: .leading)
        }
    }
}
