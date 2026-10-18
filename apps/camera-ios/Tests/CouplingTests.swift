import XCTest
import simd
@testable import BluebookCamera

final class CouplingTests: XCTestCase {
    func testBoxConversionTopLeftToVisionBottomLeft() {
        // Box in the upper-left of the upright JPEG -> upper-left in Vision's bottom-left space = high y.
        let tl = CGRect(x: 0.1, y: 0.2, width: 0.3, height: 0.1)
        let v = TrackGeo.visionBox(fromTopLeft: tl)
        XCTAssertEqual(v.minX, 0.1, accuracy: 1e-9); XCTAssertEqual(v.minY, 0.7, accuracy: 1e-9)
        XCTAssertEqual(v.width, 0.3, accuracy: 1e-9); XCTAssertEqual(v.height, 0.1, accuracy: 1e-9)
        let back = TrackGeo.topLeftBox(fromVision: v)
        XCTAssertEqual(back.minY, tl.minY, accuracy: 1e-9); XCTAssertEqual(back.height, tl.height, accuracy: 1e-9)
        // The tracker sees the buffer with .right: a landscape-buffer box maps to the same upright box (Vision does the rotation).
        XCTAssertEqual(TrackGeo.clampedUnit(CGRect(x: 0.9, y: 0.9, width: 0.3, height: 0.3)).width, 0.1, accuracy: 1e-9)
        XCTAssertTrue(TrackGeo.centreInside(tl)); XCTAssertFalse(TrackGeo.centreInside(CGRect(x: 0.95, y: 0.4, width: 0.3, height: 0.1)))
    }

    func testStateMachineHoldsReportsOnceAndReacquires() {
        var c = Coupling()
        XCTAssertEqual(c.update(conf: 0.9, inImage: true, now: 0), .init(state: .tracked, measure: true, lost: false))
        // low confidence: still tracked during the 0.5 s grace
        XCTAssertEqual(c.update(conf: 0.2, inImage: true, now: 1.0), .init(state: .tracked, measure: false, lost: false))
        XCTAssertEqual(c.update(conf: 0.2, inImage: true, now: 1.5), .init(state: .tracked, measure: false, lost: false))
        // > 0.5 s: held, "lost" exactly once
        XCTAssertEqual(c.update(conf: 0.2, inImage: true, now: 1.6), .init(state: .held, measure: false, lost: true))
        XCTAssertEqual(c.update(conf: 0.1, inImage: true, now: 2.0), .init(state: .held, measure: false, lost: false))
        // recovers
        XCTAssertEqual(c.update(conf: 0.8, inImage: true, now: 2.1), .init(state: .tracked, measure: true, lost: false))
        // leaving the image counts as bad; a second loss is reported again
        _ = c.update(conf: 0.9, inImage: false, now: 3.0)
        XCTAssertEqual(c.update(conf: 0.9, inImage: false, now: 3.6), .init(state: .held, measure: false, lost: true))
    }

    func testTrackerOnlyRefinesAPlacedPointNeverDragsIt() {
        let p: SIMD3<Float> = [0.5, -0.4, -0.6]
        XCTAssertTrue(Coupling.refines(p + [0.03, -0.02, 0.04], p))  // surface/LiDAR jitter: refine
        XCTAssertFalse(Coupling.refines([1.3, -0.39, -2.17], p))  // recorded 2026-10-18: re-acquired the wall 1.8 m behind
        XCTAssertFalse(Coupling.refines(p + [0, 0, 0.1], p))  // a box stuck in the image drags it with the phone: no
    }

    func testBlipDoesNotTriggerLoss() {
        var c = Coupling()
        _ = c.update(conf: 0.1, inImage: true, now: 0)
        XCTAssertTrue(c.update(conf: 0.9, inImage: true, now: 0.4).measure)
        XCTAssertFalse(c.update(conf: 0.1, inImage: true, now: 0.8).lost)  // grace restarts at 0.8
        XCTAssertFalse(c.update(conf: 0.1, inImage: true, now: 1.2).lost)
    }

    func testAnchorJSONFields() throws {
        let a = WorldAnchor(id: "cup", p: [1, 2, 3], how: "tracked", conf: 0.8765, ageS: 0.1234, box: nil).json
        XCTAssertEqual(a["id"] as? String, "cup"); XCTAssertEqual(a["how"] as? String, "tracked")
        XCTAssertEqual(a["p"] as? [Double], [1, 2, 3])
        XCTAssertEqual(a["conf"] as? Double ?? 0, 0.88, accuracy: 1e-9); XCTAssertEqual(a["age_s"] as? Double ?? 0, 0.12, accuracy: 1e-9)
        let cam = WorldAnchor(id: "camera", p: [0, 0, 0], how: "camera").json
        XCTAssertEqual(cam["conf"] as? Double, 1); XCTAssertEqual(cam["age_s"] as? Double, 0)
        XCTAssertNil(a["box"])
    }

    func testDiagNewFields() {
        var d = ARDiag(); d.lux = 123.456; d.features = 42; d.faceLostReason = "rear: no face detected"
        XCTAssertEqual(d.json["lux"] as? Double, 123.5); XCTAssertEqual(d.json["features"] as? Int, 42)
        XCTAssertEqual(d.json["face_lost_reason"] as? String, "rear: no face detected")
        XCTAssertTrue(ARDiag().json["face_lost_reason"] is NSNull); XCTAssertTrue(ARDiag().json["lux"] is NSNull)
        XCTAssertEqual(ARDiag.lostReason(frontTracked: false, rearNote: "no depth"), "front: not tracked; rear: no depth")
        XCTAssertEqual(ARDiag.lostReason(frontTracked: false, rearNote: nil), "front: not tracked")
    }

    func testRearROI() {
        let e = RearROI.expand(CGRect(x: 0.4, y: 0.4, width: 0.2, height: 0.2), by: 2)
        XCTAssertEqual(e.minX, 0.3, accuracy: 1e-9); XCTAssertEqual(e.width, 0.4, accuracy: 1e-9)
        XCTAssertEqual(RearROI.expand(CGRect(x: 0.8, y: 0.8, width: 0.2, height: 0.2), by: 2).maxX, 1, accuracy: 1e-9)
        let h = RearROI.head(inBody: CGRect(x: 0.2, y: 0.1, width: 0.4, height: 0.8))  // top (high y) part of the person
        XCTAssertGreaterThan(h.midY, 0.5); XCTAssertLessThanOrEqual(h.maxY, 1)
    }
}
