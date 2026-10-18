import Observation
import SwiftUI
import UIKit

/// Everything the old web debug page did, as observable state. All networking runs in main-actor tasks that only
/// *await* URLSession (the work happens off-thread), never on the capture/pose queue.
@MainActor @Observable
final class GlassesModel {
    var reachable: Bool?  // nil = not asked yet
    var sessions: [String] = []
    var status: ServerStatus?
    var rawStatus = Data()
    var mirror: UIImage?
    var skills: [SkillSummary] = []
    var steps: [SkillStep] = []
    var display = DisplayParams()
    var notice: String?  // last action error, auto-clears
    var busy = false
    var findText = ""  // the "Find" field; also the label for tap-placed anchors
    /// UI visible that wants the fast poll + mirror (card expanded or tuning sheet open).
    var detailed = false

    var session: String? { sessions.first }
    var hasServer: Bool { api != nil }
    var targets: [String] { session.flatMap { status?.sessions[$0]?.targets } ?? [] }

    private var api: ServerAPI? { Settings.httpBase(UserDefaults.standard.string(forKey: Keys.server) ?? "").map(ServerAPI.init) }
    private var loops: [Task<Void, Never>] = []
    private var lastPNG = Data()
    private var editedAt = Date.distantPast
    private var pushTask: Task<Void, Never>?
    private var noticeTask: Task<Void, Never>?

    // MARK: polling (only while foregrounded)

    func start() {
        guard loops.isEmpty else { return }
        loops = [
            loop(1.0) { await $0.pollHealth() },
            loop(nil) { await $0.pollStatus() },
            loop(0.33) { await $0.pollMirror() },
        ]
    }

    func stop() { loops.forEach { $0.cancel() }; loops = [] }

    /// Server URL changed: drop stale state and poll again right away.
    func serverChanged() {
        reachable = nil; sessions = []; status = nil; mirror = nil; lastPNG = Data(); skills = []; steps = []
        if !loops.isEmpty { stop(); start() }
    }

    private func loop(_ period: Double?, _ body: @escaping @MainActor (GlassesModel) async -> Void) -> Task<Void, Never> {
        Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let t0 = ContinuousClock.now
                await body(self)
                let p = period ?? (self.detailed ? 0.33 : 1.0)
                try? await Task.sleep(until: t0 + .seconds(p), clock: .continuous)
            }
        }
    }

    private func pollHealth() async {
        guard let api else { reachable = nil; sessions = []; return }
        do {
            let h = try await api.health()
            reachable = true
            if h.sessions != sessions { sessions = h.sessions }
        } catch { reachable = false; sessions = [] }
    }

    private func pollStatus() async {
        guard let api else { status = nil; return }
        guard let (s, raw) = try? await api.status() else { return }
        status = s; rawStatus = raw
        if let sess = session, let d = s.display[sess], Date().timeIntervalSince(editedAt) > 3 { display = d }
    }

    private func pollMirror() async {
        guard detailed, let api, let sess = session else { return }
        guard let d = try? await api.glassesPNG(sess), d != lastPNG, let img = UIImage(data: d) else { return }
        lastPNG = d; mirror = img
    }

    // MARK: actions

    private func perform(_ work: @escaping @Sendable (ServerAPI, String) async throws -> Void) {
        guard let api else { return flash("Set the server URL in settings.") }
        guard let sess = session else { return flash("No glasses connected.") }
        busy = true
        Task {
            defer { busy = false }
            do { try await work(api, sess); notice = nil; await pollStatus() } catch { flash(error.localizedDescription) }
        }
    }

    private func flash(_ s: String) {
        notice = s
        noticeTask?.cancel()
        noticeTask = Task { try? await Task.sleep(for: .seconds(6)); if !Task.isCancelled { notice = nil } }
    }

    func trackPhone() { perform { try await $0.highlight($1, name: "camera") } }
    func find(_ name: String) {
        let n = name.trimmingCharacters(in: .whitespaces)
        if !n.isEmpty { perform { try await $0.highlight($1, name: n) } }
    }
    func placeTapped() {
        let n = findText.trimmingCharacters(in: .whitespaces)
        perform { try await $0.highlight($1, anchor: "tapped", name: n.isEmpty ? "target" : n) }
    }
    func show(skill: String, step: String) { perform { try await $0.highlight($1, skill: skill, step: step) } }
    func clear() { perform { try await $0.clear($1) } }
    func realign() { perform { try await $0.align($1) } }

    func loadSkills() async {
        guard let api, let s = try? await api.skills() else { return }
        skills = s
    }

    func loadSteps(skill: String) async {
        steps = []
        guard let api else { return }
        do { steps = try await api.steps(skill: skill) } catch { flash(error.localizedDescription) }
    }

    /// Optimistic local edit, then one debounced POST (steppers can tap fast).
    func edit(_ change: (inout DisplayParams) -> Void) {
        change(&display)
        editedAt = Date()
        pushTask?.cancel()
        pushTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(300))
            guard !Task.isCancelled, let self, let api = self.api, let sess = self.session else { return }
            do { try await api.setDisplay(sess, self.display) } catch { self.flash(error.localizedDescription) }
        }
    }
}
