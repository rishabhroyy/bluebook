import Foundation

enum WSState: Sendable { case idle, connecting, connected, error }

/// One URLSessionWebSocketTask with auto-reconnect (backoff 0.5 -> 5 s). The hello text is sent first on every
/// (re)connect; other sends are dropped until then, and when too many are in flight (newest frame wins).
final class WSClient: @unchecked Sendable {
    var onState: (@Sendable (WSState) -> Void)?
    var onText: (@Sendable (String) -> Void)?

    private let lock = NSLock()
    private var url: URL?
    private var hello: String?
    private var task: URLSessionWebSocketTask?
    private var loop: Task<Void, Never>?
    private var ready = false
    private var pending = 0
    private let maxPending: Int
    private let session = URLSession(configuration: .default)

    init(maxPending: Int) { self.maxPending = maxPending }

    func start(url: URL?) {
        stop()
        lock.withLock { self.url = url }
        loop = Task { await self.run() }
    }

    func stop() {
        loop?.cancel(); loop = nil
        lock.withLock { ready = false; task?.cancel(with: .goingAway, reason: nil); task = nil; hello = nil }
        onState?(.idle)
    }

    /// Set (or change) the hello; a change drops the connection so it is re-sent on reconnect.
    func setHello(_ s: String) {
        lock.withLock { if hello != s { hello = s; ready = false; task?.cancel(with: .goingAway, reason: nil) } }
    }

    @discardableResult func send(text: String) -> Bool { send(.string(text)) }
    @discardableResult func send(data: Data) -> Bool { send(.data(data)) }

    private func send(_ m: URLSessionWebSocketTask.Message) -> Bool {
        let t: URLSessionWebSocketTask? = lock.withLock {
            guard ready, pending < maxPending else { return nil }
            pending += 1
            return task
        }
        guard let t else { return false }
        t.send(m) { [weak self] _ in self?.lock.withLock { self?.pending -= 1 } }
        return true
    }

    private func run() async {
        var delay = 0.5
        while !Task.isCancelled {
            let (u, h) = lock.withLock { (url, hello) }
            guard let u, let h else { try? await Task.sleep(for: .milliseconds(200)); continue }
            onState?(.connecting)
            let t = session.webSocketTask(with: u)
            lock.withLock { task = t; pending = 0 }
            t.resume()
            do {
                try await t.send(.string(h))
                lock.withLock { ready = true }
                onState?(.connected)
                delay = 0.5
                while true {
                    if case .string(let s) = try await t.receive() { onText?(s) }
                }
            } catch {
                lock.withLock { ready = false }
                if Task.isCancelled { break }
                onState?(.error)
                try? await Task.sleep(for: .seconds(delay))
                delay = min(delay * 2, 5)
            }
        }
    }
}
