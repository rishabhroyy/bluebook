import XCTest
import simd
@testable import BluebookCamera

final class ProjectionTests: XCTestCase {
    private let proj = WorldProjector(K: Intrinsics(fx: 1000, fy: 1000, cx: 960, cy: 540), landscape: CGSize(width: 1920, height: 1080),
                                      camT: matrix_identity_float4x4)

    func testHeadHalfMetreAheadLandsAtCentreAndZAxisForeshortens() throws {
        // Rear camera looks down -z; head 0.5 m ahead, +z (out of the face) pointing back at the camera.
        let h = WorldHead(R: matrix_identity_float3x3, t: [0, 0, -0.5])
        let hp = try XCTUnwrap(HeadProjection.make(h, proj))
        XCTAssertEqual(hp.eye.x, 0.5, accuracy: 1e-4); XCTAssertEqual(hp.eye.y, 0.5, accuracy: 1e-4)
        XCTAssertEqual(hp.depth, 0.5, accuracy: 1e-5)
        // tip of +z is nearer the camera (depth 0.35) yet on the optical axis: length collapses to ~0 px (foreshortened)
        let z = try XCTUnwrap(hp.axes[2]), x = try XCTUnwrap(hp.axes[0])
        XCTAssertLessThan(proj.depth(h.t + h.R[2] * 0.15), 0.5)
        let px = { (s: (CGPoint, CGPoint)) in hypot((s.1.x - s.0.x) * 1080, (s.1.y - s.0.y) * 1920) }
        XCTAssertLessThan(px(z), 1)
        XCTAssertEqual(px(x), 300, accuracy: 1)  // fx * 0.15 / 0.5
        // the gaze ray runs to 0.1 m from the camera, still in front
        XCTAssertNotNil(hp.gaze)
    }

    func testHeadBehindCameraGivesNoProjection() {
        XCTAssertNil(HeadProjection.make(WorldHead(R: matrix_identity_float3x3, t: [0, 0, 0.5]), proj))
    }

    func testSphereRadiusHalvesWhenDistanceDoubles() {
        let r1 = proj.radiusPx(ObjectStyle.radius, depth: 1), r2 = proj.radiusPx(ObjectStyle.radius, depth: 2)
        XCTAssertEqual(r1, 50, accuracy: 1e-3)
        XCTAssertEqual(r2, r1 / 2, accuracy: 1e-3)
    }

    func testLabelAndColor() {
        XCTAssertEqual(ObjectStyle.label(id: "a/cup", distM: 0.612, how: "tracked", conf: 0.8), "cup · 61 cm · tracked · 0.80")
        XCTAssertEqual(ObjectStyle.label(id: "tapped", distM: 1, how: "lidar", conf: 1), "tapped · 100 cm · lidar")
        XCTAssertEqual(ObjectStyle.color(how: "held"), .orange)
    }
}
