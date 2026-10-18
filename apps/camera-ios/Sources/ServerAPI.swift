import Foundation

// MARK: models (all decoding is lenient: optional fields, unknown keys ignored)

extension KeyedDecodingContainer {
    /// Decode `key` or nil, never throwing (a malformed sub-block must not break the rest of the status).
    func lenient<T: Decodable>(_ key: Key) -> T? { (try? decodeIfPresent(T.self, forKey: key)) ?? nil }
}

struct Health: Decodable, Sendable {
    var ok: Bool?
    var sessions: [String] = []
    enum CodingKeys: String, CodingKey { case ok, sessions }
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        ok = c.lenient(.ok); sessions = c.lenient(.sessions) ?? []
    }
}

struct PhoneDir: Decodable, Sendable {
    var horizontal: String?, vertical: String?, ahead: Bool?
    var text: String? {
        guard let h = horizontal, let v = vertical else { return nil }
        return ahead == false ? "phone is behind you" : "phone is \(h) · \(v)"
    }
}

struct Conventions: Decodable, Sendable {
    var using: String?
    var disagreement: [String: Double] = [:]
    enum CodingKeys: String, CodingKey { case using, disagreement = "disagreement_deg" }
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        using = c.lenient(.using); disagreement = c.lenient(.disagreement) ?? [:]
    }
}

struct PoseStatus: Decodable, Sendable {
    var appConnected: Bool?, source: String?, device: String?, hz: Double?, ageS: Double?
    var size: [Double]?, K: [Double]?, faceConf: Double?, headOk: Bool?, headZ: Double?
    var phoneFromHead: PhoneDir?, arkitTruth: Bool?, conventions: Conventions?
    enum CodingKeys: String, CodingKey {
        case appConnected = "app_connected", source, device, hz, ageS = "age_s", size, K
        case faceConf = "face_conf", headOk = "head_ok", headZ = "head_z"
        case phoneFromHead = "phone_from_head", arkitTruth = "arkit_truth", conventions
    }
    init() {}
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        appConnected = c.lenient(.appConnected); source = c.lenient(.source); device = c.lenient(.device)
        hz = c.lenient(.hz); ageS = c.lenient(.ageS); size = c.lenient(.size); K = c.lenient(.K)
        faceConf = c.lenient(.faceConf); headOk = c.lenient(.headOk); headZ = c.lenient(.headZ)
        phoneFromHead = c.lenient(.phoneFromHead); arkitTruth = c.lenient(.arkitTruth)
        conventions = c.lenient(.conventions)
    }
}

/// Eye/display offset (metres, head frame) + optics, as served by /interactive/display.
struct DisplayParams: Decodable, Sendable, Equatable {
    var fovDeg = 21.4, dx = 0.0, dy = 0.0, dz = 0.0, cx = 288.0, cy = 144.0
    var rot: [Double] = []
    var alignedT: Double?
    enum CodingKeys: String, CodingKey { case fovDeg = "fov_deg", dx, dy, dz, cx, cy, rot, alignedT = "aligned_t" }
    init() {}
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        fovDeg = c.lenient(.fovDeg) ?? fovDeg; dx = c.lenient(.dx) ?? 0; dy = c.lenient(.dy) ?? 0; dz = c.lenient(.dz) ?? 0
        cx = c.lenient(.cx) ?? cx; cy = c.lenient(.cy) ?? cy; rot = c.lenient(.rot) ?? []; alignedT = c.lenient(.alignedT)
    }
}

struct SessionStep: Decodable, Sendable {
    var step: String?
    var targets: [String] = []
    enum CodingKeys: String, CodingKey { case step, targets }
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        step = c.lenient(.step); targets = c.lenient(.targets) ?? []
    }
}

struct IMUSummary: Decodable, Sendable {
    var n: Int?, hz: Double?, last: [Double]?, min: [Double]?, max: [Double]?
}

struct ServerStatus: Decodable, Sendable {
    var pose = PoseStatus()
    var omegaDegS: Double?, latencyP50Ms: Double?
    var display: [String: DisplayParams] = [:]
    var sessions: [String: SessionStep] = [:]
    var imu: [String: IMUSummary] = [:]
    enum CodingKeys: String, CodingKey {
        case pose, omegaDegS = "omega_deg_s", latencyP50Ms = "latency_p50_ms", display, sessions, imu
    }
    init() {}
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        pose = c.lenient(.pose) ?? PoseStatus()
        omegaDegS = c.lenient(.omegaDegS); latencyP50Ms = c.lenient(.latencyP50Ms)
        display = c.lenient(.display) ?? [:]; sessions = c.lenient(.sessions) ?? [:]; imu = c.lenient(.imu) ?? [:]
    }
}

struct SkillSummary: Decodable, Sendable, Identifiable, Hashable {
    var id: String
    var title: String?, summary: String?
}

struct SkillStep: Decodable, Sendable, Identifiable, Hashable {
    var id: String
    var title: String?, tooltip: String?
    var label: String { title ?? tooltip ?? id }
}

private struct SkillDetail: Decodable, Sendable {
    var steps: [SkillStep] = []
    enum CodingKeys: String, CodingKey { case steps }
    init(from d: Decoder) throws {
        steps = (try? d.container(keyedBy: CodingKeys.self).decodeIfPresent([LossySkillStep].self, forKey: .steps))?
            .compactMap(\.value) ?? []
    }
}
/// A step without an id is skipped instead of failing the whole skill.
private struct LossySkillStep: Decodable, Sendable {
    var value: SkillStep?
    init(from d: Decoder) throws { value = try? SkillStep(from: d) }
}

// MARK: endpoints

struct ServerAPI: Sendable {
    let base: URL

    enum Failure: LocalizedError {
        case unreachable, http(Int, String?), badResponse
        var errorDescription: String? {
            switch self {
            case .unreachable: "Can't reach the server."
            case .http(404, let d): d.map { "Not found: \($0)" } ?? "Not found."
            case .http(let c, let d): d ?? "Server error (\(c))."
            case .badResponse: "Unexpected reply from the server."
            }
        }
    }

    private static let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.waitsForConnectivity = false
        c.timeoutIntervalForRequest = 4
        return URLSession(configuration: c)
    }()
    private static let strict = CharacterSet.alphanumerics.union(.init(charactersIn: "-._~"))
    static func esc(_ s: String) -> String { s.addingPercentEncoding(withAllowedCharacters: strict) ?? s }

    func data(_ path: String, post: Bool = false, query: [(String, String)] = [], timeout: TimeInterval = 4) async throws -> Data {
        var s = base.absoluteString + path
        if !query.isEmpty { s += "?" + query.map { "\($0.0)=\(Self.esc($0.1))" }.joined(separator: "&") }
        guard let url = URL(string: s) else { throw Failure.badResponse }
        var req = URLRequest(url: url, timeoutInterval: timeout)
        if post { req.httpMethod = "POST" }
        let (data, resp): (Data, URLResponse)
        do { (data, resp) = try await Self.session.data(for: req) } catch { throw Failure.unreachable }
        guard let h = resp as? HTTPURLResponse else { throw Failure.badResponse }
        guard (200..<300).contains(h.statusCode) else {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            throw Failure.http(h.statusCode, detail)
        }
        return data
    }

    private func decode<T: Decodable>(_ t: T.Type, _ d: Data) throws -> T {
        do { return try JSONDecoder().decode(t, from: d) } catch { throw Failure.badResponse }
    }

    func health() async throws -> Health { try decode(Health.self, await data("/health")) }
    /// Raw bytes too, for the JSON viewer.
    func status() async throws -> (ServerStatus, Data) {
        let d = try await data("/interactive/status"); return (try decode(ServerStatus.self, d), d)
    }
    func glassesPNG(_ s: String) async throws -> Data { try await data("/interactive/glasses.png", query: [("session", s)]) }
    func skills() async throws -> [SkillSummary] { try decode([SkillSummary].self, await data("/skills")) }
    func steps(skill: String) async throws -> [SkillStep] {
        try decode(SkillDetail.self, await data("/skills/\(Self.esc(skill))")).steps
    }

    private func stepPath(_ s: String) -> String { "/interactive/step/\(Self.esc(s))" }
    func clear(_ s: String) async throws { _ = try await data(stepPath(s), post: true) }
    func highlight(_ s: String, name: String) async throws {
        _ = try await data(stepPath(s), post: true, query: [("name", name)], timeout: 40)  // VLM lookup can take a while
    }
    func highlight(_ s: String, skill: String, step: String) async throws {
        _ = try await data(stepPath(s), post: true, query: [("skill_id", skill), ("step_id", step)], timeout: 20)
    }
    /// Highlight an app-side world anchor (e.g. "tapped") under `name`.
    func highlight(_ s: String, anchor: String, name: String) async throws {
        _ = try await data(stepPath(s), post: true, query: [("anchor", anchor), ("name", name)], timeout: 20)
    }
    func align(_ s: String) async throws { _ = try await data("/interactive/align/\(Self.esc(s))", post: true) }
    func setDisplay(_ s: String, _ p: DisplayParams) async throws {
        let q: [(String, String)] = [("fov_deg", p.fovDeg), ("dx", p.dx), ("dy", p.dy), ("dz", p.dz), ("cx", p.cx), ("cy", p.cy)]
            .map { ($0.0, String(format: "%.4f", $0.1)) }
        _ = try await data("/interactive/display/\(Self.esc(s))", post: true, query: q)
    }
}
