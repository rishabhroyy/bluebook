import SwiftUI

/// Collapsible frosted card: live glasses mirror + "what to highlight" controls.
struct GlassesPanel: View {
    @Bindable var model: GlassesModel
    @Binding var expanded: Bool
    var onTune: () -> Void

    enum Mode: String, CaseIterable { case phone = "Phone", find = "Find", skill = "Skill" }
    @State private var mode = Mode.phone
    @State private var skillID = ""
    @State private var stepID = ""
    @FocusState private var typing: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            if let b = banner { Label(b.1, systemImage: b.0).font(.footnote).foregroundStyle(.orange) }
            if expanded {
                mirrorView
                if let t = model.status?.pose.phoneFromHead?.text, model.session != nil {
                    Label(t, systemImage: "location.north.line").font(.footnote.weight(.medium).monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                controls
                if !model.targets.isEmpty { targetChips }
                if let n = model.notice {
                    Label(n, systemImage: "exclamationmark.circle").font(.footnote).foregroundStyle(.orange)
                }
            }
        }
        .padding(14)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .animation(.snappy(duration: 0.25), value: expanded)
        .animation(.snappy(duration: 0.25), value: mode)
    }

    // MARK: header

    private var banner: (String, String)? {
        if !model.hasServer { return ("gearshape", "Set the server URL in settings.") }
        if model.reachable == false { return ("wifi.exclamationmark", "Can't reach the server. Check the URL and Tailscale.") }
        if model.reachable == true, model.session == nil {
            return ("eyeglasses", "No glasses connected — scan the QR in the Even Realities app")
        }
        return nil
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Button { expanded.toggle() } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "eyeglasses").font(.body.weight(.semibold))
                        Text(model.session.map { "G2 \($0.suffix(6))" } ?? "Glasses").font(.subheadline.weight(.semibold))
                        Circle().fill(model.session != nil ? .green : .gray).frame(width: 8, height: 8)
                        Spacer()
                        Image(systemName: "chevron.up").font(.caption.weight(.bold)).foregroundStyle(.secondary)
                            .rotationEffect(.degrees(expanded ? 0 : 180))
                    }
                    .contentShape(Rectangle())
                }.buttonStyle(.plain)
                Button(action: onTune) { Image(systemName: "slider.horizontal.3").frame(width: 32, height: 28) }
                    .accessibilityLabel("Tuning")
            }
            if model.session != nil, let p = model.status?.pose { chips(p) }
        }
    }

    private func chips(_ p: PoseStatus) -> some View {
        HStack(spacing: 6) {
            if p.appConnected == false {
                chip("iphone.slash", "phone app offline", .orange)
            } else {
                chip("camera.metering.center.weighted", p.source == "arkit" ? "ARKit" : (p.source?.hasPrefix("arkit-world") == true ? "World" : (p.source?.capitalized ?? "—")))
                chip("speedometer", String(format: "%.0f Hz", p.hz ?? 0))
                chip(p.headOk == true ? "checkmark.circle.fill" : "exclamationmark.triangle.fill",
                     p.headOk == true ? "head" : "head lost", p.headOk == true ? .green : .orange)
                if let l = model.status?.latencyP50Ms { chip("timer", String(format: "%.0f ms", l)) }
            }
        }
    }

    private func chip(_ icon: String, _ text: String, _ tint: Color = .secondary) -> some View {
        Label(text, systemImage: icon).font(.caption2.weight(.semibold).monospacedDigit()).foregroundStyle(tint)
            .lineLimit(1).padding(.horizontal, 8).padding(.vertical, 4).background(.white.opacity(0.08), in: Capsule())
    }

    // MARK: mirror

    private var mirrorView: some View {
        ZStack {
            Color.black
            if let img = model.mirror, model.session != nil {
                Image(uiImage: img).resizable().interpolation(.none).aspectRatio(2, contentMode: .fit)
            } else {
                Image(systemName: "eyeglasses").font(.largeTitle).foregroundStyle(.green.opacity(0.35))
            }
        }
        .aspectRatio(2, contentMode: .fit)
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(.white.opacity(0.12)))
    }

    // MARK: controls

    private var controls: some View {
        VStack(spacing: 10) {
            Picker("Highlight", selection: $mode) { ForEach(Mode.allCases, id: \.self) { Text($0.rawValue) } }
                .pickerStyle(.segmented)
            switch mode {
            case .phone:
                action("Track the phone", "iphone.radiowaves.left.and.right") { model.trackPhone() }
            case .find:
                HStack(spacing: 8) {
                    TextField("hydro flask", text: $model.findText)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().submitLabel(.go)
                        .focused($typing).onSubmit { typing = false; model.find(model.findText) }
                        .padding(.horizontal, 12).padding(.vertical, 10)
                        .background(.white.opacity(0.1), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    action("Show", "scope") { typing = false; model.find(model.findText) }
                        .disabled(model.findText.trimmingCharacters(in: .whitespaces).isEmpty)
                        .fixedSize()
                }
            case .skill:
                skillPickers
            }
            Button(role: .destructive) { model.clear() } label: {
                Label("Clear", systemImage: "xmark.circle").font(.subheadline.weight(.medium)).frame(maxWidth: .infinity)
            }.buttonStyle(.bordered).tint(.red).disabled(model.session == nil || model.busy)
        }
    }

    private func action(_ title: String, _ icon: String, _ run: @escaping () -> Void) -> some View {
        Button(action: run) {
            HStack(spacing: 6) {
                if model.busy { ProgressView().controlSize(.small).tint(.white) } else { Image(systemName: icon) }
                Text(title)
            }
            .font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity).padding(.vertical, 10)
            .padding(.horizontal, 6)
            .background(Color.accentColor, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .foregroundStyle(.white)
        }
        .disabled(model.session == nil || model.busy)
        .opacity(model.session == nil ? 0.4 : 1)
    }

    private var skillPickers: some View {
        VStack(spacing: 8) {
            HStack {
                Picker("Skill", selection: $skillID) {
                    Text("Choose a skill").tag("")
                    ForEach(model.skills) { Text($0.title ?? $0.id).tag($0.id) }
                }
                Spacer()
                Picker("Step", selection: $stepID) {
                    Text("Step").tag("")
                    ForEach(model.steps) { Text($0.label).tag($0.id) }
                }.disabled(skillID.isEmpty)
            }
            .pickerStyle(.menu).labelsHidden().tint(.white)
            action("Show step", "list.number") { model.show(skill: skillID, step: stepID) }
                .disabled(skillID.isEmpty || stepID.isEmpty)
        }
        .task { await model.loadSkills() }
        .onChange(of: skillID) { _, id in
            stepID = ""
            if !id.isEmpty { Task { await model.loadSteps(skill: id); stepID = model.steps.first?.id ?? "" } }
        }
    }

    private var targetChips: some View {
        HStack(spacing: 6) {
            Image(systemName: "scope").font(.caption).foregroundStyle(.green)
            ForEach(model.targets, id: \.self) { t in
                Text(t.replacingOccurrences(of: "_", with: " ")).font(.caption.weight(.semibold))
                    .padding(.horizontal, 8).padding(.vertical, 3).background(.green.opacity(0.2), in: Capsule())
            }
            Spacer()
        }
    }
}
