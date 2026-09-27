import SwiftUI

struct SettingsView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        Form {
            Section {
                Toggle("Open at Login", isOn: $model.launchAtLogin)
            }

            Section {
                Picker("Scaling", selection: $model.scaling) {
                    ForEach(Scaling.allCases) { Text($0.title).tag($0) }
                }
                Toggle("Play Sound", isOn: $model.playAudio)
            } header: {
                Text("Playback")
            } footer: {
                Text(model.scaling == .fill
                     ? "Fill Screen trims the edges of videos shaped differently from your display."
                     : "Fit to Screen shows the whole video, softly extending it to the edges.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            Section {
                Toggle("When the desktop is hidden", isOn: $model.pauseWhenCovered)
                Toggle("On battery power", isOn: $model.pauseOnBattery)
            } header: {
                Text("Pause Automatically")
            } footer: {
                Text("Reverie always pauses while your Mac is locked, asleep, or in Low Power Mode.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            Section {
                Toggle("Match desktop picture", isOn: $model.matchDesktopPicture)
            } footer: {
                Text("Uses a still from your video as the desktop picture, so the lock screen and Mission Control match.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .scrollDisabled(true)
        .frame(width: 440)
        .fixedSize(horizontal: false, vertical: true)
    }
}
