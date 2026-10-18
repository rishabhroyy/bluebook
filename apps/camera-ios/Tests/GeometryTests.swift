import XCTest
import simd
@testable import BluebookCamera

final class GeometryTests: XCTestCase {
    func testHeadOnIsDiag1m1m1() {
        XCTAssertTrue(Geo.selfCheck())
        let r = Geo.rowMajor(Geo.rotation(roll: 0, yaw: 0, pitch: 0))
        XCTAssertEqual(r, [1, 0, 0, 0, -1, 0, 0, 0, -1])
        XCTAssertEqual(simd_determinant(Geo.headOnR), 1, accuracy: 1e-6)
    }

    func testVisionDepth() {
        // 63 mm IPD at 1 m with f = 1000 px -> 63 px apart -> z = 1 m, centred pixel -> x = y = 0.
        let K = Intrinsics(fx: 1000, fy: 1000, cx: 540, cy: 960)
        let t = Geo.position(left: CGPoint(x: 508.5, y: 960), right: CGPoint(x: 571.5, y: 960), K: K, yaw: 0, ipd: 0.063)!
        XCTAssertEqual(t.z, 1, accuracy: 1e-3); XCTAssertEqual(t.x, 0, accuracy: 1e-3)
        // Yawed 60 deg: pupils 31.5 px apart but we divide by cos(yaw) -> still 1 m.
        let t2 = Geo.position(left: CGPoint(x: 524.25, y: 960), right: CGPoint(x: 555.75, y: 960), K: K, yaw: .pi / 3, ipd: 0.063)!
        XCTAssertEqual(t2.z, 1, accuracy: 1e-3)
    }

    func testProjectOrigin() {
        let K = Intrinsics(fx: 1000, fy: 1000, cx: 540, cy: 960)
        let p = Geo.project([0, 0, 0], HeadPose(R: Geo.headOnR, t: [0, 0, 1]), K)!
        XCTAssertEqual(p.x, 540, accuracy: 1e-3); XCTAssertEqual(p.y, 960, accuracy: 1e-3)
        // head +x (wearer's left) lands on image right
        let q = Geo.project([0.1, 0, 0], HeadPose(R: Geo.headOnR, t: [0, 0, 1]), K)!
        XCTAssertGreaterThan(q.x, 540)
    }
}

final class ServerAPITests: XCTestCase {
    func testStatusDecodesFull() throws {
        let json = """
        {"pose":{"app_connected":true,"source":"arkit","device":"iPhone","hz":29.8,"age_s":0.1,"size":[720,1280],
        "K":[1,2,3,4],"face_conf":0.9,"head_ok":true,"head_z":0.4,
        "phone_from_head":{"horizontal":"12° LEFT","vertical":"4° DOWN","ahead":true},"arkit_truth":true,
        "conventions":{"using":"A","disagreement_deg":{"A":0.0,"B":3.5}}},
        "omega_deg_s":10.5,"latency_p50_ms":87,
        "display":{"g2":{"fov_deg":21.4,"dx":0.01,"dy":0,"dz":0,"cx":288,"cy":144,"rot":[0,0,0],"aligned_t":1.5}},
        "sessions":{"g2":{"step":"adhoc","targets":["phone"]}},"anchors":{},"tracks":{},
        "imu":{"g2":{"n":10,"hz":100,"last":[1,2,3],"min":[0,0,0],"max":[1,1,1]}},"future_key":1}
        """
        let s = try JSONDecoder().decode(ServerStatus.self, from: Data(json.utf8))
        XCTAssertEqual(s.pose.source, "arkit")
        XCTAssertEqual(s.pose.phoneFromHead?.text, "phone is 12° LEFT · 4° DOWN")
        XCTAssertEqual(s.pose.conventions?.disagreement["B"], 3.5)
        XCTAssertEqual(s.latencyP50Ms, 87)
        XCTAssertEqual(s.display["g2"]?.dx, 0.01)
        XCTAssertEqual(s.sessions["g2"]?.targets, ["phone"])
        XCTAssertEqual(s.imu["g2"]?.n, 10)
    }

    func testStatusDecodesMissingAndNulls() throws {
        let json = #"{"pose":{"app_connected":false,"source":null,"hz":0,"head_z":null,"phone_from_head":null},"display":{},"sessions":{}}"#
        let s = try JSONDecoder().decode(ServerStatus.self, from: Data(json.utf8))
        XCTAssertEqual(s.pose.appConnected, false)
        XCTAssertNil(s.pose.source); XCTAssertNil(s.pose.phoneFromHead); XCTAssertNil(s.latencyP50Ms)
        XCTAssertTrue(s.imu.isEmpty)
        // wrong-typed sub-block is dropped, not fatal
        let bad = try JSONDecoder().decode(ServerStatus.self, from: Data(#"{"pose":"x","omega_deg_s":2}"#.utf8))
        XCTAssertEqual(bad.omegaDegS, 2)
        XCTAssertEqual(try JSONDecoder().decode(ServerStatus.self, from: Data("{}".utf8)).sessions.count, 0)
    }

    func testHealthAndSkillsAndBase() throws {
        let h = try JSONDecoder().decode(Health.self, from: Data(#"{"ok":true,"parts":["x"],"sessions":["A1"]}"#.utf8))
        XCTAssertEqual(h.sessions, ["A1"])
        XCTAssertEqual(Settings.httpBase("https://x.ts.net/foo")?.absoluteString, "https://x.ts.net")
        XCTAssertEqual(Settings.httpBase("http://127.0.0.1:8799")?.absoluteString, "http://127.0.0.1:8799")
        XCTAssertEqual(Settings.httpBase("wss://x.ts.net")?.absoluteString, "https://x.ts.net")
        XCTAssertNil(Settings.httpBase(" "))
        XCTAssertEqual(ServerAPI.esc("hydro flask&1"), "hydro%20flask%261")
    }
}

final class WorldTests: XCTestCase {
    func testWorldHeadSelfCheck() { XCTAssertTrue(Geo.worldSelfCheck()) }

    func testWorldHeadKnownTransform() {
        // Face anchor = identity rotation at (0.5, 1.2, -0.8); eyes at +-3.2 cm in x, 1 cm up, 3 cm out.
        var f = matrix_identity_float4x4; f.columns.3 = [0.5, 1.2, -0.8, 1]
        var l = matrix_identity_float4x4, r = matrix_identity_float4x4
        l.columns.3 = [0.032, 0.01, 0.03, 1]; r.columns.3 = [-0.032, 0.01, 0.03, 1]
        let h = Geo.worldHead(faceT: f, leftEye: l, rightEye: r)
        XCTAssertEqual(Geo.rowMajor(h.R), [1, 0, 0, 0, 1, 0, 0, 0, 1])
        XCTAssertLessThan(simd_length(h.t - [0.5, 1.21, -0.77]), 1e-6)
        // yaw 90 deg about +y (right-handed): face +z -> world +x
        let c: Float = 0, sn: Float = 1
        var g = matrix_identity_float4x4
        g.columns.0 = [c, 0, -sn, 0]; g.columns.1 = [0, 1, 0, 0]; g.columns.2 = [sn, 0, c, 0]
        let h2 = Geo.worldHead(faceT: g, leftEye: l, rightEye: r)
        XCTAssertLessThan(simd_length(h2.R.columns.2 - [1, 0, 0]), 1e-6)
        XCTAssertLessThan(simd_length(h2.t - [0.03, 0.01, 0]), 1e-6)  // eye mid (0,.01,.03) -> (.03,.01,0)
        XCTAssertEqual(simd_determinant(h2.R), 1, accuracy: 1e-5)
    }

    func testPortraitLandscapeNormRoundTripAndIntrinsics() {
        let p = CGPoint(x: 0.2, y: 0.7)
        let q = Geo.portraitNorm(fromLandscape: Geo.landscapeNorm(fromPortrait: p))
        XCTAssertEqual(q.x, p.x, accuracy: 1e-9); XCTAssertEqual(q.y, p.y, accuracy: 1e-9)
        // landscape 1920x1440 K; pixel (u,v) -> portrait (H-v, u); K rotation must agree.
        let K = Intrinsics(fx: 1500, fy: 1490, cx: 960, cy: 720)
        let Kp = K.rotatedToPortrait(landscapeH: 1440)
        XCTAssertEqual(Kp.cx, 1440 - 720); XCTAssertEqual(Kp.cy, 960); XCTAssertEqual(Kp.fx, 1490); XCTAssertEqual(Kp.fy, 1500)
        // a camera-space point (x right, y up in landscape ARKit): landscape pixel then portrait pixel via both routes
        let x: Float = 0.3, y: Float = 0.1, d: Float = 2
        let u = K.fx * x / d + K.cx, v = -K.fy * y / d + K.cy
        let (xp, yp) = (1440 - v, u)  // rotate pixel
        // portrait OpenCV camera: x_p = y_ar, y_p = x_ar, z = d
        XCTAssertEqual(Kp.fx * y / d + Kp.cx, xp, accuracy: 1e-3)
        XCTAssertEqual(Kp.fy * x / d + Kp.cy, yp, accuracy: 1e-3)
        let n = Geo.portraitNorm(fromLandscape: CGPoint(x: Double(u / 1920), y: Double(v / 1440)))
        XCTAssertEqual(Float(n.x) * 1440, xp, accuracy: 1e-2); XCTAssertEqual(Float(n.y) * 1920, yp, accuracy: 1e-2)
    }

    func testCIImageRightRotationMatchesPixelFormula() {
        // 8x4 black landscape image with a white pixel at (u=1, v=0) (top-left origin). After .right it must sit at (H-1-v, u) = (3, 1).
        let (w, h) = (8, 4)
        var px = [UInt8](repeating: 0, count: w * h * 4)
        for i in 0..<w * h { px[i * 4 + 3] = 255 }
        px[(0 * w + 1) * 4] = 255; px[(0 * w + 1) * 4 + 1] = 255; px[(0 * w + 1) * 4 + 2] = 255
        let cg = CGDataProvider(data: Data(px) as CFData).flatMap {
            CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: w * 4,
                    space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
                    provider: $0, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
        }!
        let img = CIImage(cgImage: cg).oriented(.right)
        XCTAssertEqual(img.extent.width, 4); XCTAssertEqual(img.extent.height, 8)
        var out = [UInt8](repeating: 0, count: 4 * 8 * 4)
        CIContext().render(img, toBitmap: &out, rowBytes: 4 * 4, bounds: img.extent, format: .RGBA8, colorSpace: CGColorSpaceCreateDeviceRGB())
        let white = (0..<32).filter { out[$0 * 4] > 200 }.map { ($0 % 4, $0 / 4) }
        XCTAssertEqual(white.count, 1)
        XCTAssertTrue(white.first! == (3, 1), "white pixel at \(white)")
    }

    func testUnprojectProjectRoundTrip() {
        let K = Intrinsics(fx: 1500, fy: 1500, cx: 960, cy: 720)
        var cam = matrix_identity_float4x4  // camera at (1,1,1), yawed 90 deg about y: looks toward -x
        cam.columns.0 = [0, 0, -1, 0]; cam.columns.2 = [1, 0, 0, 0]; cam.columns.3 = [1, 1, 1, 1]
        let w = Geo.unprojectARKit(u: 1200, v: 500, depth: 1.5, K: K, camT: cam)
        let q = Geo.projectARKit(w, K: K, camT: cam)!
        XCTAssertEqual(q.u, 1200, accuracy: 1e-2); XCTAssertEqual(q.v, 500, accuracy: 1e-2); XCTAssertEqual(q.depth, 1.5, accuracy: 1e-4)
        // optical axis hits depth straight ahead: camera looks along -z_cam = -(1,0,0)... = (-1,0,0)
        let c = Geo.unprojectARKit(u: 960, v: 720, depth: 2, K: K, camT: cam)
        XCTAssertLessThan(simd_length(c - [-1, 1, 1]), 1e-5)
    }

    func testRobustCentreDropsOutliers() {
        var pts = (0..<20).map { i in SIMD3<Float>(0.5 + Float(i % 3) * 0.002, 0.2, -1.0 + Float(i % 5) * 0.002) }
        pts += [[3, 3, 3], [-2, 0, 5], [0.5, 0.2, -4]]
        let c = Geo.robustCentre(pts)!
        XCTAssertLessThan(simd_length(c - [0.502, 0.2, -0.996]), 0.01)
        XCTAssertNil(Geo.robustCentre([]))
    }

    func testViewToImageNorm() {
        // 9:16 image in a taller 400x900 view: aspect-fill scale = max(400/900, 900/1600) = .5625 -> image 506x900, cropped horizontally
        let img = CGSize(width: 900, height: 1600)
        let p = Geo.viewToImageNorm(CGPoint(x: 200, y: 450), viewSize: CGSize(width: 400, height: 900), imageSize: img)!
        XCTAssertEqual(p.x, 0.5, accuracy: 1e-6); XCTAssertEqual(p.y, 0.5, accuracy: 1e-6)
        let o = Geo.viewToImageNorm(.zero, viewSize: CGSize(width: 400, height: 900), imageSize: img)!
        XCTAssertGreaterThan(o.x, 0)
    }
}

final class RearFaceTests: XCTestCase {
    private func close(_ a: SIMD3<Float>, _ b: SIMD3<Float>, _ e: Float = 1e-4) -> Bool { simd_length(a - b) < e }

    func testRearHeadOnFacesPhone() {
        // identity camera (looks along -z); wearer in front looking back: head +z = camera +z (toward the phone)
        let R = Geo.rearWorldRotation(camT: matrix_identity_float4x4, rCV: Geo.headOnR)
        XCTAssertTrue(close(R.columns.2, [0, 0, 1]))   // out of face -> toward phone
        XCTAssertTrue(close(R.columns.1, [-1, 0, 0]))  // head up = upright-image up = ARKit -x (landscape)
        XCTAssertTrue(close(R.columns.0, [0, 1, 0]))   // wearer's left = upright image right = ARKit +y
        XCTAssertEqual(simd_determinant(R), 1, accuracy: 1e-5)
        // same orientation as the front-anchor convention (selfCheck face frame)
        XCTAssertTrue(Geo.selfCheck())
        // phone rotated 90 deg about world y (looks along -x): head +z must follow the camera +z = world (1,0,0)
        var cam = matrix_identity_float4x4
        cam.columns.0 = [0, 0, -1, 0]; cam.columns.2 = [1, 0, 0, 0]
        XCTAssertTrue(close(Geo.rearWorldRotation(camT: cam, rCV: Geo.headOnR).columns.2, [1, 0, 0]))
    }

    func testUprightLandscapePixelMapping() {
        // upright (x_p, y_p) in a 1440x1920 portrait image <-> landscape (u, v) of 1920x1440
        let (u, v) = Geo.landscapePixel(fromUpright: CGPoint(x: 300, y: 1000), landscapeH: 1440)
        XCTAssertEqual(u, 1000); XCTAssertEqual(v, 1140)
        let n = Geo.portraitNorm(fromLandscape: CGPoint(x: Double(u) / 1920, y: Double(v) / 1440))
        XCTAssertEqual(n.x * 1440, 300, accuracy: 1e-3); XCTAssertEqual(n.y * 1920, 1000, accuracy: 1e-3)
        // same answer as the normalised helper used for LiDAR placement
        let l = Geo.landscapeNorm(fromPortrait: CGPoint(x: 300.0 / 1440, y: 1000.0 / 1920))
        XCTAssertEqual(Float(l.x) * 1920, u, accuracy: 1e-2); XCTAssertEqual(Float(l.y) * 1440, v, accuracy: 1e-2)
    }

    func testPatchDepthToWorld() {
        let (w, h) = (16, 12)
        var d = [Float](repeating: 1.5, count: w * h)
        for i in 0..<9 { d[(h / 2 + i % 3) * w + w / 2 + i / 3] = 0 }  // holes
        d[5 * w + 5] = 4.0                                              // outlier
        var conf = [UInt8](repeating: 2, count: w * h)
        for x in 0..<w { conf[(h / 2 - 1) * w + x] = 0 }                // low confidence row (ignored)
        let z = d.withUnsafeBufferPointer { dp in conf.withUnsafeBufferPointer { cp in
            Geo.patchMedianDepth(depth: dp.baseAddress!, stride: w, conf: cp.baseAddress!, confStride: w, w: w, h: h,
                                 centre: CGPoint(x: 0.5, y: 0.5), radius: 3, minConf: 1)
        } }
        XCTAssertEqual(z!, 1.5, accuracy: 1e-5)
        // all-invalid -> nil
        let none = [Float](repeating: 0, count: w * h)
        XCTAssertNil(none.withUnsafeBufferPointer { Geo.patchMedianDepth(depth: $0.baseAddress!, stride: w, conf: nil, confStride: 0, w: w, h: h, centre: CGPoint(x: 0.5, y: 0.5)) })
        // principal point, identity cam: 1.5 m ahead (-z) + 1.5 cm along the ray
        let K = Intrinsics(fx: 1500, fy: 1500, cx: 960, cy: 720)
        XCTAssertTrue(close(Geo.rearOrigin(u: 960, v: 720, depth: 1.5, K: K, camT: matrix_identity_float4x4), [0, 0, -1.515]))
        // upright-right of centre => ARKit camera +y (landscape up): pixel is 100 px right in the upright image
        let (u, v) = Geo.landscapePixel(fromUpright: CGPoint(x: 720 + 100, y: 960), landscapeH: 1440)
        let p = Geo.rearOrigin(u: u, v: v, depth: 1, K: K, camT: matrix_identity_float4x4, eyeOffset: 0)
        XCTAssertGreaterThan(p.y, 0.06); XCTAssertEqual(p.x, 0, accuracy: 1e-5)
        // camera moved: world = camT * point
        var cam = matrix_identity_float4x4; cam.columns.3 = [1, 2, 3, 1]
        XCTAssertTrue(close(Geo.rearOrigin(u: 960, v: 720, depth: 2, K: K, camT: cam, eyeOffset: 0), [1, 2, 1]))
        // IPD fallback agrees with the depth path for the same point
        let tCV: SIMD3<Float> = [0, 0, 2]
        XCTAssertTrue(close(Geo.rearOriginIPD(tCV: tCV, camT: matrix_identity_float4x4), [0, 0, -2]))
    }

    func testFaceSourceHysteresis() {
        var s = FaceSourceSelector()
        XCTAssertNil(s.update(frontTracked: false, rearAvailable: false))
        XCTAssertEqual(s.update(frontTracked: false, rearAvailable: true), .rear)
        // front flickers back: stays rear until 3 consecutive tracked frames
        XCTAssertEqual(s.update(frontTracked: true, rearAvailable: true), .rear)
        XCTAssertEqual(s.update(frontTracked: true, rearAvailable: true), .rear)
        XCTAssertEqual(s.update(frontTracked: false, rearAvailable: true), .rear)  // streak reset
        XCTAssertEqual(s.update(frontTracked: true, rearAvailable: true), .rear)
        XCTAssertEqual(s.update(frontTracked: true, rearAvailable: true), .rear)
        XCTAssertEqual(s.update(frontTracked: true, rearAvailable: true), .front)
        XCTAssertFalse(s.wantsRear)
        // front lost again -> immediate rear if available
        XCTAssertEqual(s.update(frontTracked: false, rearAvailable: true), .rear)
        // front first, no rear
        var f = FaceSourceSelector()
        XCTAssertEqual(f.update(frontTracked: true, rearAvailable: false), .front)
    }
}
