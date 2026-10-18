import Foundation

/// Picks which ARFaceAnchor to use each frame. ARKit may drop a face anchor and add a NEW one (new identifier) after a brief
/// loss, and `frame.anchors` can briefly hold a stale untracked one next to it, so never cache an identifier and never take
/// `.first`: prefer tracked anchors, and among those the one first seen most recently (ties: later in the array).
struct FaceBook {
    struct Candidate: Equatable { var id: UUID; var tracked: Bool }
    private var firstSeen: [UUID: Double] = [:]
    private(set) var lastTracked: Double?
    private(set) var pickedID: UUID?

    mutating func pick(_ c: [Candidate], now: Double) -> UUID? {
        let ids = Set(c.map(\.id))
        firstSeen = firstSeen.filter { ids.contains($0.key) }
        for x in c where firstSeen[x.id] == nil { firstSeen[x.id] = now }
        let best = c.enumerated().filter { $0.element.tracked }
            .max { (firstSeen[$0.element.id]!, $0.offset) < (firstSeen[$1.element.id]!, $1.offset) }?.element.id
        if best != nil { lastTracked = now }
        pickedID = best
        return best
    }

    /// Seconds since a face was last tracked (nil: never since reset).
    func age(now: Double) -> Double? { lastTracked.map { max(now - $0, 0) } }

    mutating func reset() { self = FaceBook() }
}

/// Per-frame ARKit health, sent in every world pose message as `ar`.
struct ARDiag: Sendable, Equatable {
    var tracking = "notAvailable"  // "normal" | "limited:<reason>" | "notAvailable"
    var faces = 0  // ARFaceAnchor count in frame.anchors
    var faceTracked = false
    var faceAge: Double?  // s since last tracked face; nil = never
    var fps = 0.0  // ARKit camera frame rate
    var faceSrc: String?  // "front" | "rear" | nil: which sensor the head pose came from
    var lux: Double?  // ARLightEstimate.ambientIntensity
    var features: Int?  // raw feature points in this frame
    var faceLostReason: String?  // best effort, only while no head pose

    /// "front: not tracked; rear: no face detected" style explanation of a missing head pose.
    static func lostReason(frontTracked: Bool, rearNote: String?) -> String {
        ([frontTracked ? nil : "front: not tracked", rearNote.map { "rear: \($0)" }].compactMap { $0 }).joined(separator: "; ")
    }

    var json: [String: Any] {
        var o: [String: Any] = ["tracking": tracking, "faces": faces, "face_tracked": faceTracked, "fps": (fps * 10).rounded() / 10]
        o["face_src"] = faceSrc ?? NSNull()
        o["lux"] = lux.map { ($0 * 10).rounded() / 10 } ?? NSNull()
        o["features"] = features ?? NSNull()
        o["face_lost_reason"] = faceLostReason ?? NSNull()
        o["face_age_s"] = faceAge.map { ($0 * 100).rounded() / 100 } ?? NSNull()
        return o
    }
}
