import SwiftUI

struct SettingsView: View {
    @AppStorage(Keys.server) private var server = ""
    @AppStorage(Keys.cameraId) private var cameraId = "scene"
    @AppStorage(Keys.ipdMM) private var ipdMM = 63.0
    @AppStorage(Keys.sendFPS) private var sendFPS = 10.0
    @Environment(\.dismiss) private var dismiss

    private var serverOK: Bool { Settings.wsURL(server, path: "/") != nil }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://your-laptop.your-tailnet.ts.net", text: $server)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    TextField("Camera id", text: $cameraId).textInputAutocapitalization(.never).autocorrectionDisabled()
                } header: { Text("Server") } footer: {
                    Text((serverOK ? "" : "Enter your server URL to start streaming. ") + "http(s) or ws(s) accepted. Streams to /ws/camera and /interactive/ws/pose. Changes apply immediately while streaming.")
                }
                Section {
                    LabeledContent("Interpupillary distance") { Text("\(Int(ipdMM)) mm").monospacedDigit() }
                    Slider(value: $ipdMM, in: 50...75, step: 1)
                    LabeledContent("JPEG send rate") { Text("\(Int(sendFPS)) fps").monospacedDigit() }
                    Slider(value: $sendFPS, in: 1...30, step: 1)
                } header: { Text("Tracking") } footer: {
                    Text("IPD sets metric depth on the Vision path (z = f·IPD / pupil distance). ARKit measures depth itself.")
                }
            }
            .navigationTitle("Settings")
            .toolbar { Button("Done") { dismiss() } }
        }
        .presentationDetents([.medium, .large])
    }
}
