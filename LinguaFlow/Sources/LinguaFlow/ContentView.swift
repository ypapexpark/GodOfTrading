import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var settings: AppSettings

    var body: some View {
        ZStack {
            LinearGradient(
                colors: [
                    Color(red: 0.055, green: 0.065, blue: 0.095),
                    Color(red: 0.08, green: 0.095, blue: 0.145)
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            .ignoresSafeArea()

            ScrollView {
                VStack(spacing: 18) {
                    header
                    actionPicker
                    optionsBar
                    editorCard

                    if let error = model.errorMessage {
                        noticeCard(message: error, symbol: "exclamationmark.triangle.fill", color: .orange)
                    } else if let message = model.transientMessage {
                        noticeCard(message: message, symbol: "checkmark.circle.fill", color: .green)
                    }

                    if model.isProcessing {
                        processingCard
                    } else if let result = model.result {
                        resultCard(result)
                    }
                }
                .padding(.horizontal, 28)
                .padding(.top, 24)
                .padding(.bottom, 30)
            }
        }
        .preferredColorScheme(.dark)
    }

    private var header: some View {
        HStack(spacing: 14) {
            ZStack {
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(
                        LinearGradient(
                            colors: [Color.cyan, Color.indigo],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        )
                    )
                Image(systemName: "character.bubble.fill")
                    .font(.system(size: 23, weight: .semibold))
                    .foregroundStyle(.white)
            }
            .frame(width: 48, height: 48)

            VStack(alignment: .leading, spacing: 3) {
                Text("LinguaFlow")
                    .font(.system(size: 23, weight: .bold, design: .rounded))
                Text("어디서든 선택하고, 바로 자연스럽게")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            Label("선택한 문장만 전송", systemImage: "lock.shield.fill")
                .font(.caption.weight(.medium))
                .foregroundStyle(.mint)
                .padding(.horizontal, 11)
                .padding(.vertical, 7)
                .background(.mint.opacity(0.1), in: Capsule())

            SettingsLink {
                Image(systemName: "gearshape.fill")
                    .font(.system(size: 15, weight: .semibold))
                    .frame(width: 30, height: 30)
            }
            .buttonStyle(.borderless)
            .help("설정")
        }
    }

    private var actionPicker: some View {
        HStack(spacing: 8) {
            ForEach(WritingAction.allCases) { action in
                Button {
                    withAnimation(.easeOut(duration: 0.16)) {
                        model.action = action
                        model.result = nil
                    }
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: action.symbol)
                        Text(action.title)
                        Text(action.shortcutDescription)
                            .font(.caption2.monospaced())
                            .foregroundStyle(model.action == action ? .white.opacity(0.65) : .secondary)
                    }
                    .font(.subheadline.weight(.semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 11)
                    .contentShape(Rectangle())
                    .background {
                        if model.action == action {
                            RoundedRectangle(cornerRadius: 11, style: .continuous)
                                .fill(Color.indigo.opacity(0.82))
                        }
                    }
                }
                .buttonStyle(.plain)
            }
        }
        .padding(5)
        .background(.white.opacity(0.055), in: RoundedRectangle(cornerRadius: 15, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 15, style: .continuous)
                .stroke(.white.opacity(0.07))
        }
    }

    private var optionsBar: some View {
        HStack(spacing: 12) {
            if model.action == .translate {
                Menu {
                    ForEach(LanguageOption.supported) { language in
                        Button("\(language.flag)  \(language.name)") {
                            settings.targetLanguageCode = language.code
                        }
                    }
                } label: {
                    optionLabel(
                        title: "번역 언어",
                        value: "\(settings.targetLanguage.flag) \(settings.targetLanguage.name)",
                        symbol: "globe"
                    )
                }
                .menuStyle(.borderlessButton)
                .frame(maxWidth: 220)
            } else if model.action == .rewrite {
                Menu {
                    ForEach(WritingTone.allCases) { tone in
                        Button(tone.title) { settings.tone = tone }
                    }
                } label: {
                    optionLabel(title: "문장 톤", value: settings.tone.title, symbol: "slider.horizontal.3")
                }
                .menuStyle(.borderlessButton)
                .frame(maxWidth: 220)
            }

            Spacer()

            if let source = model.sourceAppName {
                Label(source, systemImage: "app.dashed")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            Button {
                model.loadClipboard()
            } label: {
                Label("클립보드 불러오기", systemImage: "clipboard")
            }
            .buttonStyle(.borderless)
            .font(.caption.weight(.medium))
        }
        .frame(minHeight: 34)
    }

    private func optionLabel(title: String, value: String, symbol: String) -> some View {
        HStack(spacing: 9) {
            Image(systemName: symbol)
                .foregroundStyle(.cyan)
            VStack(alignment: .leading, spacing: 1) {
                Text(title)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Text(value)
                    .font(.caption.weight(.semibold))
            }
            Spacer(minLength: 8)
            Image(systemName: "chevron.down")
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 7)
        .background(.white.opacity(0.055), in: RoundedRectangle(cornerRadius: 10))
    }

    private var editorCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("원문")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
                    .textCase(.uppercase)
                Spacer()
                Text("\(model.inputText.count)자")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.tertiary)
            }

            TextEditor(text: $model.inputText)
                .font(.system(size: 16, weight: .regular, design: .default))
                .lineSpacing(4)
                .scrollContentBackground(.hidden)
                .frame(minHeight: 130, maxHeight: 220)
                .overlay(alignment: .topLeading) {
                    if model.inputText.isEmpty {
                        Text("다른 앱에서 문장을 선택한 뒤 단축키를 누르거나, 여기에 직접 입력하세요.")
                            .font(.system(size: 15))
                            .foregroundStyle(.tertiary)
                            .padding(.top, 1)
                            .allowsHitTesting(false)
                    }
                }

            Divider().overlay(.white.opacity(0.08))

            HStack {
                Button("비우기", systemImage: "xmark") {
                    model.clear()
                }
                .buttonStyle(.borderless)
                .foregroundStyle(.secondary)

                Spacer()

                Button {
                    Task { await model.transform() }
                } label: {
                    HStack(spacing: 8) {
                        if model.isProcessing {
                            ProgressView().controlSize(.small)
                        } else {
                            Image(systemName: model.action.symbol)
                        }
                        Text(model.action.title)
                    }
                    .font(.subheadline.weight(.bold))
                    .padding(.horizontal, 18)
                    .padding(.vertical, 9)
                }
                .buttonStyle(.plain)
                .background(
                    LinearGradient(colors: [.indigo, .blue], startPoint: .leading, endPoint: .trailing),
                    in: RoundedRectangle(cornerRadius: 10, style: .continuous)
                )
                .disabled(model.isProcessing || model.inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .opacity(model.isProcessing ? 0.7 : 1)
                .keyboardShortcut(.return, modifiers: [.command])
            }
        }
        .padding(18)
        .background(.white.opacity(0.055), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .stroke(.white.opacity(0.075))
        }
    }

    private var processingCard: some View {
        HStack(spacing: 14) {
            ProgressView().controlSize(.regular)
            VStack(alignment: .leading, spacing: 2) {
                Text("문장을 다듬고 있습니다")
                    .font(.subheadline.weight(.semibold))
                Text("의미와 고유명사, 숫자를 보존해 처리합니다.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(18)
        .background(.indigo.opacity(0.1), in: RoundedRectangle(cornerRadius: 16))
    }

    private func resultCard(_ result: WritingResult) -> some View {
        VStack(alignment: .leading, spacing: 15) {
            HStack {
                Label("결과", systemImage: "sparkles")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.cyan)
                    .textCase(.uppercase)
                Spacer()
                Text("감지 언어  \(result.detectedLanguage.uppercased())")
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
            }

            Text(result.result)
                .font(.system(size: 17))
                .lineSpacing(5)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)

            if !result.changes.isEmpty {
                Divider().overlay(.white.opacity(0.08))
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(result.changes.prefix(4)) { change in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(change.before)
                                .strikethrough()
                                .foregroundStyle(.red.opacity(0.85))
                            Image(systemName: "arrow.right")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                            Text(change.after)
                                .foregroundStyle(.green)
                            Spacer(minLength: 10)
                            Text(change.reason)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }

            Divider().overlay(.white.opacity(0.08))

            HStack(spacing: 10) {
                Button("다시 편집", systemImage: "arrow.up.left") {
                    model.useResultAsInput()
                }
                .buttonStyle(.borderless)

                Spacer()

                Button("복사", systemImage: "doc.on.doc") {
                    model.copyResult()
                }
                .buttonStyle(.bordered)

                Button("원문 교체", systemImage: "arrow.triangle.2.circlepath") {
                    Task { await model.replaceOriginalSelection() }
                }
                .buttonStyle(.borderedProminent)
                .tint(.indigo)
            }
        }
        .padding(18)
        .background(.cyan.opacity(0.055), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .stroke(.cyan.opacity(0.18))
        }
    }

    private func noticeCard(message: String, symbol: String, color: Color) -> some View {
        HStack(spacing: 10) {
            Image(systemName: symbol).foregroundStyle(color)
            Text(message)
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .background(color.opacity(0.08), in: RoundedRectangle(cornerRadius: 12))
    }
}
