import CoreGraphics
import Foundation
import simd

/// Pinhole intrinsics in pixels of an upright image (origin top-left).
struct Intrinsics: Equatable, Sendable {
    var fx: Float, fy: Float, cx: Float, cy: Float
    func scaled(_ s: Float) -> Intrinsics { .init(fx: fx * s, fy: fy * s, cx: cx * s, cy: cy * s) }
    /// Sensor-landscape intrinsics -> intrinsics of the same image rotated 90 deg clockwise (landscape -> portrait).
    /// `landscapeH` is the landscape image height (= portrait width).
    func rotatedToPortrait(landscapeH: Float) -> Intrinsics { .init(fx: fy, fy: fx, cx: landscapeH - cy, cy: cx) }
}

/// Head pose: X_cam = R * X_head + t.
///
/// CONVENTIONS (the server depends on these)
///  * Camera frame = OpenCV on the upright, UNMIRRORED sent image: x right, y down, z forward (metres).
///  * Head frame: origin at the midpoint of the eyes, +x = wearer's LEFT, +y = UP, +z = out of the face
///    (the direction the wearer looks).
///  * Head-on derivation: the wearer faces the camera, so their left appears on the image's right
///    (head +x = cam +x), head up = image up (head +y = cam -y), and "out of the face" points back at the
///    camera (head +z = cam -z).  R = diag(1, -1, -1), det = +1 (a proper rotation).
struct HeadPose: Sendable {
    var R: simd_float3x3
    var t: SIMD3<Float>
    /// ARKit only: the phone's camera in the head frame, computed inside ARKit's own (consistent) world with no axis
    /// conversions: inverse(face) * camera, minus the eye midpoint. Ground truth for the phone target and for
    /// checking R/t's conventions on the server.
    var camInHead: SIMD3<Float>? = nil
}

enum Geo {
    static let headOnR = simd_float3x3(diagonal: [1, -1, -1])

    // MARK: Vision path

    /// Vision's image frame (normalised coords have y up): x right, y up, z toward the viewer, right-handed.
    /// Wearer facing the camera head-on: head +x (their left) = image right = +x_v, head +y = +y_v,
    /// head +z (toward camera) = +z_v, so R_vision(head-on) = I.  Vision -> OpenCV is diag(1,-1,-1).
    static let visionToCV = simd_float3x3(diagonal: [1, -1, -1])

    /// Sign knobs for Vision's roll/yaw/pitch. Apple documents only "rotation about the z / y / x axis" with no
    /// sign convention, so these assume right-hand-rule rotations of the face about Vision's axes above:
    ///  +yaw   : face's +z (nose) swings toward +x_v (image right) -> wearer turns to THEIR LEFT.
    ///  +pitch : face's +z swings toward -y_v (down)... right-hand about +x: +z -> -y, i.e. nodding down.
    ///  +roll  : counter-clockwise in the image.
    /// UNVERIFIED on hardware: flip a sign here if the on-screen axes (red x, green y, blue z) move the wrong way.
    static let visionSigns = (roll: Float(1), yaw: Float(1), pitch: Float(1))

    static func rotation(roll: Float, yaw: Float, pitch: Float) -> simd_float3x3 {
        let (cr, sr) = (cos(roll * visionSigns.roll), sin(roll * visionSigns.roll))
        let (cy, sy) = (cos(yaw * visionSigns.yaw), sin(yaw * visionSigns.yaw))
        let (cp, sp) = (cos(pitch * visionSigns.pitch), sin(pitch * visionSigns.pitch))
        let rz = simd_float3x3(rows: [[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]])
        let ry = simd_float3x3(rows: [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
        let rx = simd_float3x3(rows: [[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
        return visionToCV * (rz * ry * rx)
    }

    /// Eye midpoint back-projected at z = f * IPD / (pupil_px / cos(yaw)). Pixels: upright image, origin top-left.
    static func position(left: CGPoint, right: CGPoint, K: Intrinsics, yaw: Float, ipd: Float) -> SIMD3<Float>? {
        let d = Float(hypot(left.x - right.x, left.y - right.y)) / max(cos(yaw), 0.3)
        guard d > 1 else { return nil }
        let z = (K.fx + K.fy) / 2 * ipd / d
        let u = Float(left.x + right.x) / 2, v = Float(left.y + right.y) / 2
        return [(u - K.cx) / K.fx * z, (v - K.cy) / K.fy * z, z]
    }

    // MARK: ARKit path

    /// ARKit camera (landscape-right sensor frame: x right, y up, z backward) -> OpenCV on the image rotated 90 deg
    /// clockwise to portrait. Step 1, ARKit -> OpenCV landscape: diag(1,-1,-1). Step 2, rotating the image 90 deg CW
    /// maps pixel (u,v) -> (H-v, u), so x_p = -y_cv, y_p = x_cv, z_p = z_cv. Product (x_p=y_ar, y_p=x_ar, z_p=-z_ar):
    static let arkitToUpright = simd_float3x3(rows: [[0, 1, 0], [1, 0, 0], [0, 0, -1]])

    /// ARKit face frame (+x = face's left, +y up, +z out of the face) already equals our head frame, so only the
    /// camera side is converted. `faceT` is face->world, `camT` camera->world, eye transforms are face-relative.
    static func arkitPose(camT: simd_float4x4, faceT: simd_float4x4, leftEye: simd_float4x4,
                          rightEye: simd_float4x4) -> HeadPose {
        let m = camT.inverse * faceT  // face -> ARKit camera
        let rf = simd_float3x3(m.columns.0.xyz, m.columns.1.xyz, m.columns.2.xyz)
        let o = (leftEye.columns.3.xyz + rightEye.columns.3.xyz) / 2  // head origin in face frame
        let t = rf * o + m.columns.3.xyz
        // Hardware tests 2026-10-17: the head POSITION is right (the preview marker sits on the face), but the
        // ROTATION comes out left-right mirrored (yaw/roll flipped: phone direction read left/right swapped, up/down
        // fine). Conjugate the rotation only (R -> M R M; stays proper, head-on unchanged); leave t alone.
        let M = simd_float3x3(diagonal: [-1, 1, 1])
        let camInHead = (faceT.inverse * camT).columns.3.xyz - o  // face axes == head axes (Apple's ARFaceAnchor doc)
        return HeadPose(R: M * (arkitToUpright * rf) * M, t: arkitToUpright * t, camInHead: camInHead)
    }

    static func arkitIntrinsics(_ k: simd_float3x3, landscapeH: Float) -> Intrinsics {
        Intrinsics(fx: k[0][0], fy: k[1][1], cx: k[2][0], cy: k[2][1]).rotatedToPortrait(landscapeH: landscapeH)
    }

    // MARK: Shared

    /// Project a head-frame point to pixels.
    static func project(_ p: SIMD3<Float>, _ pose: HeadPose, _ K: Intrinsics) -> CGPoint? {
        let c = pose.R * p + pose.t
        guard c.z > 0.05 else { return nil }
        return CGPoint(x: Double(K.fx * c.x / c.z + K.cx), y: Double(K.fy * c.y / c.z + K.cy))
    }

    /// Row-major [r00,r01,r02,r10,...].
    static func rowMajor(_ r: simd_float3x3) -> [Float] {
        (0..<3).flatMap { row in (0..<3).map { col in r[col][row] } }
    }

    /// Intrinsics delivered by AVFoundation (sensor-landscape, arbitrary resolution) -> upright portrait buffer pixels.
    /// Normalises resolution assuming principal point ~ image centre, then rotates if the matrix is landscape.
    static func normalizeCaptureIntrinsics(_ k: simd_float3x3, bufferW: Int, bufferH: Int) -> Intrinsics {
        var i = Intrinsics(fx: k[0][0], fy: k[1][1], cx: k[2][0], cy: k[2][1])
        let landscape = i.cx > i.cy
        let longSide = Float(max(bufferW, bufferH)), shortSide = Float(min(bufferW, bufferH))
        i = i.scaled((landscape ? longSide : shortSide) / (2 * i.cx))
        return (landscape && bufferH > bufferW) ? i.rotatedToPortrait(landscapeH: shortSide) : i
    }

    /// Self-check of the head-on convention; used by the unit tests and as a DEBUG launch assertion.
    static func selfCheck() -> Bool {
        func close(_ a: simd_float3x3, _ b: simd_float3x3) -> Bool {
            (0..<3).allSatisfy { c in simd_length(a[c] - b[c]) < 1e-4 }
        }
        // Vision, all angles 0
        guard close(rotation(roll: 0, yaw: 0, pitch: 0), headOnR) else { return false }
        // ARKit: identity camera, face 1 m ahead, upright in the portrait image:
        // face up = -x_ar, face left = +y_ar, face out = +z_ar.
        var f = matrix_identity_float4x4
        f.columns.0 = [0, 1, 0, 0]; f.columns.1 = [-1, 0, 0, 0]; f.columns.2 = [0, 0, 1, 0]; f.columns.3 = [0, 0, -1, 1]
        let p = arkitPose(camT: matrix_identity_float4x4, faceT: f, leftEye: matrix_identity_float4x4,
                          rightEye: matrix_identity_float4x4)
        return close(p.R, headOnR) && simd_length(p.t - [0, 0, 1]) < 1e-4
    }
}

// MARK: World tracking (ARKit world frame: metres, gravity-aligned, NOT mirrored)

/// Head -> world pose from the user-face anchor. R = face anchor rotation (+x face's left, +y up, +z out of the face),
/// t = eye midpoint (face-relative eye transforms) mapped to world. Raw ARKit values, no mirroring.
struct WorldHead: Sendable {
    var R: simd_float3x3; var t: SIMD3<Float>
    var src: FaceSrc = .front
    var conf: Float = 1
    var lidar = true  // rear only: false = IPD-based distance
}

extension Geo {
    static func worldHead(faceT: simd_float4x4, leftEye: simd_float4x4, rightEye: simd_float4x4) -> WorldHead {
        let o = (leftEye.columns.3.xyz + rightEye.columns.3.xyz) / 2
        let R = simd_float3x3(faceT.columns.0.xyz, faceT.columns.1.xyz, faceT.columns.2.xyz)
        return WorldHead(R: R, t: (faceT * SIMD4<Float>(o.x, o.y, o.z, 1)).xyz)
    }

    /// Upright (portrait, rotated .right) normalised point -> ARKit sensor-landscape normalised point. Pixel (u,v) of the
    /// landscape image lands at (H-v, u) in the portrait one, so portrait_n = (1-v_n, u_n).
    static func landscapeNorm(fromPortrait p: CGPoint) -> CGPoint { CGPoint(x: p.y, y: 1 - p.x) }
    static func portraitNorm(fromLandscape p: CGPoint) -> CGPoint { CGPoint(x: 1 - p.y, y: p.x) }

    /// Landscape pixel + z-depth (metres along the optical axis) -> world. ARKit camera: x right, y up, z backward.
    static func unprojectARKit(u: Float, v: Float, depth d: Float, K: Intrinsics, camT: simd_float4x4) -> SIMD3<Float> {
        (camT * SIMD4<Float>((u - K.cx) / K.fx * d, -(v - K.cy) / K.fy * d, -d, 1)).xyz
    }

    /// World -> landscape pixel (+ depth), nil if behind the camera.
    static func projectARKit(_ w: SIMD3<Float>, K: Intrinsics, camT: simd_float4x4) -> (u: Float, v: Float, depth: Float)? {
        let c = (camT.inverse * SIMD4<Float>(w.x, w.y, w.z, 1)).xyz
        let d = -c.z
        guard d > 0.05 else { return nil }
        return (K.fx * c.x / d + K.cx, -K.fy * c.y / d + K.cy, d)
    }

    /// Row-major 16 floats of a 4x4 (simd is column-major).
    static func rowMajor(_ m: simd_float4x4) -> [Float] { (0..<4).flatMap { r in (0..<4).map { c in m[c][r] } } }

    /// Median of the 3D hits after dropping those further than 2x MAD (of the distances) from the first median.
    static func robustCentre(_ pts: [SIMD3<Float>]) -> SIMD3<Float>? {
        guard !pts.isEmpty else { return nil }
        func med(_ a: [Float]) -> Float { let s = a.sorted(), n = s.count; return n % 2 == 1 ? s[n / 2] : (s[n / 2 - 1] + s[n / 2]) / 2 }
        func medPt(_ p: [SIMD3<Float>]) -> SIMD3<Float> { [med(p.map(\.x)), med(p.map(\.y)), med(p.map(\.z))] }
        let m = medPt(pts)
        let d = pts.map { simd_distance($0, m) }
        let md = med(d)
        let mad = med(d.map { abs($0 - md) })
        let keep = zip(pts, d).filter { $0.1 <= md + 2 * max(mad, 0.01) }.map(\.0)
        return medPt(keep.isEmpty ? pts : keep)
    }

    /// View point (aspect-fill preview) -> normalised point of the upright sent image; nil if outside the image.
    static func viewToImageNorm(_ p: CGPoint, viewSize v: CGSize, imageSize img: CGSize) -> CGPoint? {
        let s = max(v.width / img.width, v.height / img.height)
        let x = (p.x + (img.width * s - v.width) / 2) / (img.width * s)
        let y = (p.y + (img.height * s - v.height) / 2) / (img.height * s)
        return (0...1).contains(x) && (0...1).contains(y) ? CGPoint(x: x, y: y) : nil
    }

    /// Self-check of the world-mode math (DEBUG launch assertion + tests).
    static func worldSelfCheck() -> Bool {
        // Face 2 m in front of the world origin, yawed 90 deg about +y, eyes 3 cm either side of the face origin.
        var f = matrix_identity_float4x4
        f.columns.0 = [0, 0, -1, 0]; f.columns.1 = [0, 1, 0, 0]; f.columns.2 = [1, 0, 0, 0]; f.columns.3 = [1, 1.5, -2, 1]
        var l = matrix_identity_float4x4, r = matrix_identity_float4x4
        l.columns.3 = [0.03, 0.02, 0.04, 1]; r.columns.3 = [-0.03, 0.02, 0.04, 1]
        let h = worldHead(faceT: f, leftEye: l, rightEye: r)
        // eye mid in face = (0, .02, .04) -> world = f * that = (1 + .04, 1.5 + .02, -2)
        return simd_length(h.t - [1.04, 1.52, -2]) < 1e-5 && simd_length(h.R.columns.0 - [0, 0, -1]) < 1e-6
            && simd_length(h.R.columns.2 - [1, 0, 0]) < 1e-6
    }
}

extension SIMD4 where Scalar == Float {
    var xyz: SIMD3<Float> { [x, y, z] }
}
