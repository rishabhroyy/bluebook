import SwiftUI

/// Advanced: display model, boresight re-align, conventions, IMU, raw status JSON.
struct TuningView: View {
    var model: GlassesModel
    var anchors: [AnchorInfo] = []
    var onForget: (String) -> Void = { _ in }
    @Environment(\.dismiss) private var dismiss
    @State private var copied = false

    var body: some View {
        NavigationStack {
            Form {
                if model.session == nil {
                    Section { Label("No glasses connected — scan the QR in the Even Realities app", systemImage: "eyeglasses")
                        .foregroundStyle(.orange).font(.footnote) }
                }
                displaySection
                Section {
                    Button { model.realign() } label: { Label("Re-align (1 tap on glasses)", systemImage: "scope") }
                        .disabled(model.session == nil || model.busy)
                } footer: {
                    Text("Shows a dot on the phone; tap the glasses once while looking at it to re-measure the boresight.")
                }
                if let n = model.notice { Section { Label(n, systemImage: "exclamationmark.circle").foregroundStyle(.orange).font(.footnote) } }
                if !anchors.isEmpty { anchorsSection }
                conventionsSection
                imuSection
                rawSection
            }
            .navigationTitle("Tuning")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { Button("Done") { dismiss() } }
        }
        .presentationDetents([.medium, .large])
        .preferredColorScheme(.dark)
    }

    // MARK: display

    private var displaySection: some View {
        Section {
            row("FOV", model.display.fovDeg, "°", 0.1, 1, \.fovDeg)
            row("dx (left +)", model.display.dx * 100, " cm", 0.1, 1, \.dx, scale: 100)
            row("dy (up +)", model.display.dy * 100, " cm", 0.1, 1, \.dy, scale: 100)
            row("dz (ahead +)", model.display.dz * 100, " cm", 0.1, 1, \.dz, scale: 100)
            row("cx", model.display.cx, " px", 1, 0, \.cx)
            row("cy", model.display.cy, " px", 1, 0, \.cy)
        } header: { Text("Display model") } footer: {
            Text("Eye offset in the head frame and display optics. Changes apply live and are saved on the server.")
        }
        .disabled(model.session == nil)
    }

    private func row(_ title: String, _ value: Double, _ unit: String, _ step: Double, _ digits: Int,
                     _ key: WritableKeyPath<DisplayParams, Double>, scale: Double = 1) -> some View {
        Stepper {
            LabeledContent(title) { Text(String(format: "%.\(digits)f", value) + unit).monospacedDigit() }
        } onIncrement: { model.edit { $0[keyPath: key] += step / scale } }
          onDecrement: { model.edit { $0[keyPath: key] -= step / scale } }
    }

    private var anchorsSection: some View {
        Section("World anchors") {
            ForEach(anchors) { a in
                HStack {
                    VStack(alignment: .leading) {
                        Text(a.id).font(.subheadline.weight(.semibold))
                        Text("\(a.how) · \(String(format: "%.2f", a.dist)) m from phone").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Forget", role: .destructive) { onForget(a.id) }.buttonStyle(.bordered)
                }
            }
        }
    }

    // MARK: readouts

    private var conventionsSection: some View {
        Section("Pose conventions") {
            let c = model.status?.pose.conventions
            LabeledContent("Using", value: c?.using ?? "—")
            ForEach((c?.disagreement ?? [:]).sorted { $0.key < $1.key }, id: \.key) { k, v in
                LabeledContent(k) { Text(String(format: "%.1f°", v)).monospacedDigit() }
            }
            if let t = model.status?.pose.arkitTruth { LabeledContent("ARKit ground truth", value: t ? "yes" : "no") }
        }
    }

    private var imuSection: some View {
        Section("Glasses IMU") {
            let imu = (model.status?.imu ?? [:]).sorted { $0.key < $1.key }
            if imu.isEmpty { Text("No IMU data").foregroundStyle(.secondary) }
            ForEach(imu, id: \.key) { _, m in
                LabeledContent("Rate") { Text(String(format: "%.0f Hz · %d samples", m.hz ?? 0, m.n ?? 0)).monospacedDigit() }
                if let l = m.last { LabeledContent("Last") { Text(l.map { String(format: "%.2f", $0) }.joined(separator: "  ")).monospacedDigit() } }
            }
            if let w = model.status?.omegaDegS { LabeledContent("Head rate") { Text(String(format: "%.0f °/s", w)).monospacedDigit() } }
        }
    }

    private var pretty: String {
        guard let o = try? JSONSerialization.jsonObject(with: model.rawStatus),
              let d = try? JSONSerialization.data(withJSONObject: o, options: [.prettyPrinted, .sortedKeys])
        else { return "{}" }
        return String(decoding: d, as: UTF8.self)
    }

    private var rawSection: some View {
        Section {
            DisclosureGroup("Raw status JSON") {
                let text = pretty
                Button {
                    UIPasteboard.general.string = text
                    copied = true
                    Task { try? await Task.sleep(for: .seconds(1.5)); copied = false }
                } label: { Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc") }
                ScrollView([.horizontal, .vertical]) {
                    Text(text).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }.frame(height: 260)
            }
        }
    }
}
