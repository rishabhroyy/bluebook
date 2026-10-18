import Foundation

enum Keys {
    static let server = "serverURL", cameraId = "cameraId", ipdMM = "ipdMM", sendFPS = "sendFPS"
}

enum Settings {
    static var cameraId: String {
        let s = UserDefaults.standard.string(forKey: Keys.cameraId) ?? ""
        return s.isEmpty ? "scene" : s
    }
    static var ipd: Float { Float(UserDefaults.standard.object(forKey: Keys.ipdMM) as? Double ?? 63) / 1000 }
    static var sendFPS: Double { UserDefaults.standard.object(forKey: Keys.sendFPS) as? Double ?? 10 }

    /// "https://x.ts.net" -> wss://x.ts.net<path>; http -> ws; ws(s) kept; no scheme -> wss. Any path is dropped.
    static func wsURL(_ server: String, path: String) -> URL? {
        var s = server.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }
        if !s.contains("://") { s = "https://" + s }
        guard var c = URLComponents(string: s), c.host != nil else { return nil }
        switch c.scheme?.lowercased() {
        case "http", "ws": c.scheme = "ws"
        default: c.scheme = "wss"
        }
        c.path = path; c.query = nil; c.fragment = nil
        return c.url
    }

    /// Same server setting as `wsURL`, but the http(s) origin for REST calls: ws -> http, wss -> https.
    static func httpBase(_ server: String) -> URL? {
        var s = server.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }
        if !s.contains("://") { s = "https://" + s }
        guard var c = URLComponents(string: s), c.host != nil else { return nil }
        switch c.scheme?.lowercased() {
        case "http", "ws": c.scheme = "http"
        default: c.scheme = "https"
        }
        c.path = ""; c.query = nil; c.fragment = nil
        return c.url
    }
}
