import XCTest
import simd
@testable import BluebookCamera

final class SchematicTests: XCTestCase {
    /// Phone at the origin looking down -z (identity camera); wearer 0.5 m in front of the screen (+z), 0.1 m up, 0.2 m left of the phone (-x).
    func head(at t: SIMD3<Float>) -> WorldHead {
        // face looking at the phone: face +z points to -z world; keep +y up -> R = rotation by 180° about y
        WorldHead(R: simd_float3x3(columns: ([-1, 0, 0], [0, 1, 0], [0, 0, -1])), t: t)
    }

    func testTopAndSideSides() {
        let phone = SIMD3<Float>(0, 0, 0), h = SIMD3<Float>(0.2, 0.1, 0.5)
        let sceneTop = Schematic.Fit(Schematic.fitPoints(SceneSnapshot(head: head(at: h), camT: matrix_identity_float4x4, anchors: []), .top), size: CGSize(width: 150, height: 150))
        let pTop = sceneTop.map(Schematic.View.top.uv(phone)), hTop = sceneTop.map(Schematic.View.top.uv(h))
        XCTAssertGreaterThan(hTop.y, pTop.y)   // user is behind the phone: lower on TOP (work area is up)
        XCTAssertGreaterThan(hTop.x, pTop.x)   // +x world is screen right
        let sceneSide = Schematic.Fit(Schematic.fitPoints(SceneSnapshot(head: head(at: h), camT: matrix_identity_float4x4, anchors: []), .side), size: CGSize(width: 150, height: 150))
        let pSide = sceneSide.map(Schematic.View.side.uv(phone)), hSide = sceneSide.map(Schematic.View.side.uv(h))
        XCTAssertLessThan(hSide.x, pSide.x)    // user left of the phone on SIDE (phone looks right)
        XCTAssertLessThan(hSide.y, pSide.y)    // head 0.1 m higher -> smaller screen y
        // look direction arrow points up on TOP, right on SIDE
        let tipTop = sceneTop.map(Schematic.View.top.uv(Schematic.lookDir(matrix_identity_float4x4)))
        XCTAssertLessThan(tipTop.y, sceneTop.map([0, 0]).y)
        let tipSide = sceneSide.map(Schematic.View.side.uv(Schematic.lookDir(matrix_identity_float4x4)))
        XCTAssertGreaterThan(tipSide.x, sceneSide.map([0, 0]).x)
    }

    func testFitKeepsEverythingInside() {
        let s = SceneSnapshot(head: head(at: [0, 0, 1.2]), camT: matrix_identity_float4x4, anchors: [WorldAnchor(id: "a", p: [0.3, -0.2, -0.6], how: "lidar")])
        for v in Schematic.View.allCases {
            let size = CGSize(width: 170, height: 150), fit = Schematic.Fit(Schematic.fitPoints(s, v), size: size)
            for p in Schematic.fitPoints(s, v) {
                let q = fit.map(p)
                XCTAssertTrue(CGRect(origin: .zero, size: size).insetBy(dx: 10, dy: 10).contains(q), "\(v) \(q)")
            }
        }
    }

    func testPhoneInHeadFrame() {
        // Head at z=0.5 looking at the phone (face +z = world -z, face +x = world -x). Phone at world x=-0.1 is on the face's +x = LEFT.
        let h = head(at: [0, 0, 0.5])
        let a = Schematic.PhoneInHead(head: h, phone: [-0.1, 0, 0])
        XCTAssertEqual(a.dist, (0.01 + 0.25).squareRoot(), accuracy: 1e-5)
        XCTAssertGreaterThan(a.azimuth, 0); XCTAssertTrue(a.text.contains("LEFT"))
        XCTAssertEqual(a.elevation, 0, accuracy: 1e-6)
        let b = Schematic.PhoneInHead(head: h, phone: [0.1, -0.1, 0])
        XCTAssertTrue(b.text.contains("RIGHT") && b.text.contains("DOWN"))
    }
}

final class FaceBookTests: XCTestCase {
    let a = UUID(), b = UUID(), c = UUID()

    func testPrefersTracked() {
        var f = FaceBook()
        XCTAssertEqual(f.pick([.init(id: a, tracked: true)], now: 1), a)
        // a newer untracked anchor must not win over the tracked older one
        XCTAssertEqual(f.pick([.init(id: a, tracked: true), .init(id: b, tracked: false)], now: 2), a)
        XCTAssertNil(f.pick([.init(id: a, tracked: false), .init(id: b, tracked: false)], now: 3))
    }

    func testNewestTrackedAndReplacedAnchor() {
        var f = FaceBook()
        _ = f.pick([.init(id: a, tracked: true)], now: 1)
        // ARKit drops a and adds b (new identifier): b is picked at once
        XCTAssertEqual(f.pick([.init(id: b, tracked: true)], now: 2), b)
        // both tracked: the one seen later wins, regardless of array order
        XCTAssertEqual(f.pick([.init(id: c, tracked: true), .init(id: b, tracked: true)], now: 3), c)
        XCTAssertEqual(f.pick([.init(id: b, tracked: true), .init(id: c, tracked: true)], now: 4), c)
    }

    func testAge() {
        var f = FaceBook()
        XCTAssertNil(f.age(now: 5))
        _ = f.pick([.init(id: a, tracked: true)], now: 10)
        _ = f.pick([], now: 12.5)
        XCTAssertEqual(f.age(now: 12.5)!, 2.5, accuracy: 1e-9)
    }

    func testDiagJSON() {
        let d = ARDiag(tracking: "limited:excessiveMotion", faces: 2, faceTracked: false, faceAge: nil, fps: 59.96).json
        XCTAssertEqual(d["tracking"] as? String, "limited:excessiveMotion")
        XCTAssertEqual(d["faces"] as? Int, 2); XCTAssertEqual(d["face_tracked"] as? Bool, false)
        XCTAssertTrue(d["face_age_s"] is NSNull)
    }
}
