import SwiftUI

@main
struct BluebookCameraApp: App {
    init() {
        #if DEBUG
        assert(Geo.selfCheck(), "head-on pose convention broken")
        assert(Geo.worldSelfCheck(), "world head math broken")
        #endif
    }
    var body: some Scene {
        WindowGroup { ContentView().preferredColorScheme(.dark) }
    }
}
