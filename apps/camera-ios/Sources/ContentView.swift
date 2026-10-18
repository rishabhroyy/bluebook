import SwiftUI
import simd

struct ContentView: View {
    @State private var model = CameraModel()
    @State private var glasses = GlassesModel()
    @State private var showSettings = false
    @State private var showTuning = false
    @AppStorage("glassesExpanded") private var glassesExpanded = false
    @AppStorage("schematicOn") private var schematicOn = true
    @AppStorage("projectionOn") private var projectionOn = true
    @Environment(\.scenePhase) private var scenePhase
    @AppStorage(Keys.server) private var server = ""
    @AppStorage(Keys.cameraId) private var cameraId = ""
    @AppStorage(Keys.ipdMM) private var ipdMM = 63.0
    @AppStorage(Keys.sendFPS) private var sendFPS = 10.0
    private var serverOK: Bool { Settings.wsURL(server, path: "/") != nil }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            PreviewView(sink: model.previewSink, mirrored: model.mirrored).ignoresSafeArea()
            OverlayView(state: model.overlay, mirrored: model.mirrored, projections: projectionOn && model.mode == .world).ignoresSafeArea().allowsHitTesting(false)
            if model.mode == .world && model.running { tapLayer.ignoresSafeArea() }
            VStack {
                topBar
                GlassesPanel(model: glasses, expanded: $glassesExpanded) { showTuning = true }
                if schematicOn && model.mode == .world { SchematicCard(state: model.overlay) }
                Spacer(minLength: 8)
                panel
            }
            .padding(.horizontal, 12)
        }
        .onAppear {
            if !serverOK { showSettings = true }
            glasses.detailed = glassesExpanded
            if scenePhase == .active { glasses.start() }
        }
        .onChange(of: scenePhase) { _, p in
            if p == .active { glasses.start() } else { glasses.stop(); model.saveScene() }
        }
        .onChange(of: glassesExpanded) { _, e in glasses.detailed = e || showTuning }
        .onChange(of: showTuning) { _, t in glasses.detailed = t || glassesExpanded }
        .onChange(of: server) { glasses.serverChanged() }
        .onChange(of: model.needsServer) { _, n in if n { model.needsServer = false; showSettings = true } }
        .onChange(of: server) { model.settingsChanged() }
        .onChange(of: cameraId) { model.settingsChanged() }
        .onChange(of: ipdMM) { model.settingsChanged() }
        .onChange(of: sendFPS) { model.settingsChanged() }
        .sheet(isPresented: $showSettings) { SettingsView() }
        .sheet(isPresented: $showTuning) { TuningView(model: glasses, anchors: model.stats.anchors) { model.forget($0) } }
        .alert("Error", isPresented: .constant(model.error != nil)) {
            Button("OK") { model.error = nil }
        } message: { Text(model.error ?? "") }
    }

    /// Tap the preview to drop the "tapped" anchor at that pixel (LiDAR patch), then point the glasses at it.
    private var tapLayer: some View {
        GeometryReader { geo in
            Color.clear.contentShape(Rectangle())
                .gesture(SpatialTapGesture().onEnded { v in
                    guard let n = Geo.viewToImageNorm(v.location, viewSize: geo.size, imageSize: model.overlay.report.imageSize)
                    else { return }
                    model.tapPlace(n) { ok in
                        if ok { glasses.placeTapped() } else { glasses.notice = "No depth at that point." }
                    }
                })
        }
    }

    // MARK: top

    private var topBar: some View {
        HStack(spacing: 8) {
            Pill(label: "cam", state: model.camState)
            Pill(label: "pose", state: model.poseState)
            Spacer()
            if model.mode == .world {
                Button { projectionOn.toggle() } label: {
                    Image(systemName: "scope").font(.body.weight(.semibold)).frame(width: 40, height: 40)
                        .background(.ultraThinMaterial, in: Circle())
                }.tint(projectionOn ? .yellow : .white).accessibilityLabel("Head and object projection")
                Button { schematicOn.toggle() } label: {
                    Image(systemName: "cube.transparent").font(.body.weight(.semibold)).frame(width: 40, height: 40)
                        .background(.ultraThinMaterial, in: Circle())
                }.tint(schematicOn ? .yellow : .white).accessibilityLabel("3D schematic")
            }
            Button { showSettings = true } label: {
                Image(systemName: "gearshape.fill").font(.body.weight(.semibold)).frame(width: 40, height: 40)
                    .background(.ultraThinMaterial, in: Circle())
            }.tint(.white)
        }
    }

    // MARK: bottom panel

    private var panel: some View {
        VStack(spacing: 14) {
            Picker("Mode", selection: $model.mode) {
                ForEach(model.availableModes, id: \.self) {
                    Text($0 == .world ? "World" : $0 == .face ? "Face (ARKit)" : "Vision").tag($0)
                }
            }.pickerStyle(.segmented)

            if model.mode == .world {
                Label(model.running ? "Place: tap an object" : "Stand the phone in front of the work area, screen toward you",
                      systemImage: model.running ? "hand.tap" : "iphone.gen3.radiowaves.left.and.right")
                    .font(.footnote.weight(.medium)).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
                if model.hasSavedScene && !model.running {
                    Toggle("Restore last scene", isOn: $model.restoreScene).font(.subheadline).tint(.accentColor)
                }
            }

            if model.mode == .vision {
                Picker("Camera", selection: $model.front) {
                    Label("Back", systemImage: "camera").tag(false)
                    Label("Front", systemImage: "person.crop.square").tag(true)
                }.pickerStyle(.segmented)
            }

            if model.mode == .vision {
                if model.lenses.count > 1 {
                    HStack(spacing: 8) {
                        ForEach(model.lenses) { l in
                            Button(l.label) { model.lens = l }
                                .font(.subheadline.weight(.semibold).monospacedDigit())
                                .padding(.horizontal, 14).padding(.vertical, 7)
                                .background(model.lens == l ? Color.white : Color.white.opacity(0.15), in: Capsule())
                                .foregroundStyle(model.lens == l ? .black : .white)
                        }
                        Spacer()
                    }
                }
                HStack {
                    Image(systemName: "minus.magnifyingglass").foregroundStyle(.secondary)
                    Slider(value: $model.zoom, in: model.zoomRange)
                    Image(systemName: "plus.magnifyingglass").foregroundStyle(.secondary)
                    Text(String(format: "%.1f×", model.zoom * (model.lens?.base ?? 1)))
                        .font(.subheadline.monospacedDigit()).frame(width: 48, alignment: .trailing)
                }
            }

            if let s = model.arStatus {
                Label(s, systemImage: "exclamationmark.triangle.fill").font(.footnote.weight(.medium)).foregroundStyle(.orange)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            let r = model.stats
            HStack(spacing: 10) {
                stat("speedometer", String(format: "pose %.0f Hz", r.poseHz))
                stat("photo", String(format: "jpeg %.0f Hz", r.jpegHz))
                stat("timer", String(format: "%.0f ms", r.procMs))
                Spacer()
            }
            HStack(spacing: 10) {
                stat(r.hasFace ? "face.smiling" : "questionmark.circle",
                     r.hasFace ? "face ✓ \(faceLabel(r))" : "no face" + faceLostSuffix(r),
                     tint: r.hasFace ? .green : .secondary)
                if model.mode == .world, let ar = r.ar, ar.tracking != "normal" {
                    stat("exclamationmark.circle", ar.tracking, tint: .orange)
                }
                if model.mode == .world {
                    stat("mappin.and.ellipse", "\(r.anchors.count) anchor\(r.anchors.count == 1 ? "" : "s")")
                } else {
                    stat("scope", "\(r.tracks.count) target\(r.tracks.count == 1 ? "" : "s")")
                }
                Spacer()
            }

            if !serverOK && !model.running {
                Button { showSettings = true } label: {
                    Label("Set the server URL (⚙︎)", systemImage: "exclamationmark.triangle.fill")
                        .font(.subheadline.weight(.semibold)).foregroundStyle(.orange)
                }
            }

            Button { model.toggle() } label: {
                Label(model.running ? "Stop" : "Start streaming", systemImage: model.running ? "stop.fill" : "play.fill")
                    .font(.headline).frame(maxWidth: .infinity).padding(.vertical, 14)
                    .background(model.running ? Color.red : Color.accentColor, in: RoundedRectangle(cornerRadius: 14))
                    .foregroundStyle(.white)
            }
            .disabled(!model.running && !serverOK)
            .opacity(!model.running && !serverOK ? 0.4 : 1)
        }
        .padding(16)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
    }

    private func faceLabel(_ r: FrameReport) -> String {
        guard model.mode == .world, let h = r.scene?.head else { return model.mode == .vision ? "vision" : "arkit" }
        return h.src == .rear ? "rear (Vision+\(h.lidar ? "LiDAR" : "IPD"))" : "front"
    }

    private func faceLostSuffix(_ r: FrameReport) -> String {
        guard model.mode == .world, let ar = r.ar else { return "" }
        return String(format: " (%@ %d anchor%@)", ar.faceAge.map { String(format: "lost %.0fs,", $0) } ?? "never seen,", ar.faces, ar.faces == 1 ? "" : "s")
    }

    private func stat(_ icon: String, _ text: String, tint: Color = .secondary) -> some View {
        Label(text, systemImage: icon).font(.caption.weight(.medium).monospacedDigit()).foregroundStyle(tint)
    }
}

struct Pill: View {
    let label: String
    let state: WSState
    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 8, height: 8)
            Text(label).font(.caption.weight(.semibold))
        }
        .padding(.horizontal, 10).padding(.vertical, 6)
        .background(.ultraThinMaterial, in: Capsule())
    }
    private var color: Color {
        switch state { case .connected: .green; case .connecting: .orange; case .error: .red; case .idle: .gray }
    }
}

/// Frames are pushed straight into the layer by the pipeline (no SwiftUI diffing at 30 fps).
struct PreviewView: UIViewRepresentable {
    let sink: PreviewSink
    let mirrored: Bool
    func makeUIView(context: Context) -> UIView {
        let v = UIView()
        v.layer.contentsGravity = .resizeAspectFill
        v.layer.masksToBounds = true
        sink.layer = v.layer
        return v
    }
    func updateUIView(_ v: UIView, context: Context) {
        v.transform = mirrored ? CGAffineTransform(scaleX: -1, y: 1) : .identity  // preview only; sent frames stay unmirrored
    }
}

/// Face / target overlay. Mapping mirrors the preview: aspect-fill, flipped for the front camera.
struct OverlayView: View {
    let state: OverlayState
    let mirrored: Bool
    var projections = true

    var body: some View {
        let report = state.report
        return Canvas { ctx, size in
            let img = report.imageSize
            let s = max(size.width / img.width, size.height / img.height)
            let off = CGSize(width: (img.width * s - size.width) / 2, height: (img.height * s - size.height) / 2)
            func map(_ p: CGPoint) -> CGPoint {
                let x = p.x * img.width * s - off.width
                return CGPoint(x: mirrored ? size.width - x : x, y: p.y * img.height * s - off.height)
            }
            func rect(_ r: CGRect) -> CGRect {
                let a = map(r.origin), b = map(CGPoint(x: r.maxX, y: r.maxY))
                return CGRect(x: min(a.x, b.x), y: min(a.y, b.y), width: abs(b.x - a.x), height: abs(b.y - a.y))
            }
            if let f = report.faceBox {
                ctx.stroke(Path(roundedRect: rect(f), cornerRadius: 12), with: .color(.white.opacity(0.5)), lineWidth: 1.5)
            }
            if report.axes.count == 4 {
                let o = map(report.axes[0])
                for (i, c) in [Color.red, .green, .blue].enumerated() {
                    var p = Path(); p.move(to: o); p.addLine(to: map(report.axes[i + 1]))
                    ctx.stroke(p, with: .color(c.opacity(0.9)), style: .init(lineWidth: 2.5, lineCap: .round))
                }
            }
            if let e = report.eye {
                let p = map(e)
                ctx.fill(Path(ellipseIn: CGRect(x: p.x - 4, y: p.y - 4, width: 8, height: 8)), with: .color(.yellow))
            }
            if projections, let sc = report.scene, let pj = WorldProjector(sc) {
                drawProjections(ctx, size, sc, pj, map)
            }
            for a in report.anchors where !(projections && report.scene?.K != nil) {
                guard let n = a.pt else { continue }
                let p = map(n), tapped = a.id == "tapped"
                let r: CGFloat = tapped ? 11 : 4
                let c: Color = tapped ? .green : .mint
                ctx.fill(Path(ellipseIn: CGRect(x: p.x - r, y: p.y - r, width: 2 * r, height: 2 * r)), with: .color(c.opacity(tapped ? 0.35 : 0.9)))
                if tapped { ctx.stroke(Path(ellipseIn: CGRect(x: p.x - r, y: p.y - r, width: 2 * r, height: 2 * r)), with: .color(c), lineWidth: 2.5) }
                ctx.draw(Text(String(format: "%@ %.2fm", a.id.split(separator: "/").last.map(String.init) ?? a.id, a.dist))
                    .font(.caption2.bold()).foregroundStyle(c), at: CGPoint(x: p.x + r + 4, y: p.y), anchor: .leading)
            }
            for t in report.tracks {
                if projections, let sc = report.scene, let pj = WorldProjector(sc),
                   let a = sc.anchors.first(where: { $0.id == t.id }), let q = pj.project(a.p) {  // box centre -> projected anchor
                    var l = Path(); l.move(to: map(CGPoint(x: t.box.midX, y: t.box.midY))); l.addLine(to: map(q.n))
                    ctx.stroke(l, with: .color(.white.opacity(0.6)), lineWidth: 1)
                }
                let c: Color = t.held || t.conf < 0.3 ? .orange : .cyan
                let r = rect(t.box)
                ctx.stroke(Path(roundedRect: r, cornerRadius: 8), with: .color(c.opacity(0.9)), lineWidth: 2)
                ctx.draw(Text(t.id.split(separator: "/").last.map(String.init) ?? t.id).font(.caption2.bold()).foregroundStyle(c),
                         at: CGPoint(x: r.minX + 4, y: r.minY - 8), anchor: .leading)
            }
        }
    }

    private func drawProjections(_ ctx: GraphicsContext, _ size: CGSize, _ sc: SceneSnapshot, _ pj: WorldProjector, _ map: (CGPoint) -> CGPoint) {
        let img = state.report.imageSize
        let px = max(size.width / img.width, size.height / img.height)  // view points per image pixel
        let cam = sc.camT.columns.3.xyz
        func line(_ a: CGPoint, _ b: CGPoint, _ c: Color, _ w: CGFloat, dash: [CGFloat] = []) {
            var p = Path(); p.move(to: map(a)); p.addLine(to: map(b))
            ctx.stroke(p, with: .color(c), style: .init(lineWidth: w, lineCap: .round, dash: dash))
        }
        // head
        if let h = sc.head {
            if let hp = HeadProjection.make(h, pj) {
                for (i, c) in [Color.red, .green, .blue].enumerated() { if let s = hp.axes[i] { line(s.0, s.1, c.opacity(0.9), 2.5) } }
                if let g = hp.gaze { line(g.0, g.1, Schematic.headColor(h).opacity(0.9), 1.5, dash: [6, 5]) }
                let e = map(hp.eye), col = Schematic.headColor(h)
                ctx.fill(Path(ellipseIn: CGRect(x: e.x - 5, y: e.y - 5, width: 10, height: 10)), with: .color(col))
                ctx.draw(Text("head · \(Schematic.srcLabel(h))").font(.caption2.bold()).foregroundStyle(col), at: CGPoint(x: e.x + 9, y: e.y - 10), anchor: .leading)
            } else {
                ctx.draw(Text(HeadProjection.badge(h, phone: cam)).font(.caption2.bold().monospacedDigit()).foregroundStyle(.white),
                         at: CGPoint(x: size.width - 12, y: size.height - 12), anchor: .bottomTrailing)
            }
        }
        // objects
        for a in sc.anchors {
            let c = ObjectStyle.color(how: a.how), d = simd_distance(a.p, cam)
            guard let q = pj.project(a.p) else {  // behind the camera: arrow at the border
                let u = pj.screenDirection(a.p), ctr = CGPoint(x: size.width / 2, y: size.height / 2)
                let k = min((ctr.x - 28) / max(abs(u.x), 1e-3), (ctr.y - 120) / max(abs(u.y), 1e-3))
                let tip = CGPoint(x: ctr.x + u.x * k, y: ctr.y + u.y * k), n = CGPoint(x: -u.y, y: u.x)
                var p = Path()
                p.move(to: tip); p.addLine(to: CGPoint(x: tip.x - u.x * 18 + n.x * 9, y: tip.y - u.y * 18 + n.y * 9))
                p.addLine(to: CGPoint(x: tip.x - u.x * 18 - n.x * 9, y: tip.y - u.y * 18 - n.y * 9)); p.closeSubpath()
                ctx.fill(p, with: .color(c))
                ctx.draw(Text(String(format: "%@ · %.0f cm", a.id.split(separator: "/").last.map(String.init) ?? a.id, d * 100)).font(.caption2.bold()).foregroundStyle(c),
                         at: CGPoint(x: tip.x - u.x * 24, y: tip.y - u.y * 24), anchor: .center)
                continue
            }
            let p = map(q.n), r = max(pj.radiusPx(ObjectStyle.radius, depth: q.depth) * px, 3)
            ctx.stroke(Path(ellipseIn: CGRect(x: p.x - r, y: p.y - r, width: 2 * r, height: 2 * r)), with: .color(c.opacity(0.9)), lineWidth: 1.5)
            for (i, ac) in [Color.red, .green, .blue].enumerated() {  // world axes: x red, y green, z blue
                var e = SIMD3<Float>(repeating: 0); e[i] = ObjectStyle.axisLen
                if let s = pj.segment(a.p, a.p + e) { line(s.0, s.1, ac.opacity(0.9), 1.5) }
            }
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - 3, y: p.y - 3, width: 6, height: 6)), with: .color(c))
            ctx.draw(Text(ObjectStyle.label(id: a.id, distM: d, how: a.how, conf: a.conf)).font(.caption2.bold()).foregroundStyle(c),
                     at: CGPoint(x: p.x + r + 4, y: p.y), anchor: .leading)
        }
    }
}
