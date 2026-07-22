import AppKit
import Combine
import Foundation

@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

    @Published var action: WritingAction = .translate
    @Published var inputText = ""
    @Published var result: WritingResult?
    @Published var isProcessing = false
    @Published var errorMessage: String?
    @Published var sourceAppName: String?
    @Published var transientMessage: String?

    let settings = AppSettings.shared

    private let selectionService = TextSelectionService()
    private let hotKeyManager = GlobalHotKeyManager()
    private var selectionContext: TextSelectionContext?
    private var hasStarted = false

    func start() {
        guard !hasStarted else { return }
        hasStarted = true
        hotKeyManager.start { [weak self] action in
            self?.captureFromHotKey(action: action)
        }
    }

    func showWindow() {
        WindowCoordinator.shared.show(model: self)
    }

    func captureFromHotKey(action: WritingAction) {
        self.action = action
        errorMessage = nil
        transientMessage = nil
        do {
            let capture = try selectionService.captureSelection()
            inputText = capture.text
            selectionContext = capture.context
            sourceAppName = capture.context.applicationName
            result = nil
            showWindow()
            if settings.autoRunFromHotKey, settings.apiKey() != nil {
                Task { await transform() }
            }
        } catch {
            showWindow()
            present(error)
        }
    }

    func loadClipboard() {
        guard let clipboard = selectionService.clipboardText(),
              !clipboard.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            present(LinguaFlowError.emptyInput)
            return
        }
        inputText = clipboard
        selectionContext = nil
        sourceAppName = "클립보드"
        result = nil
        errorMessage = nil
    }

    func requestAccessibilityPermission() {
        selectionService.requestAccessibilityPermission()
    }

    func transform() async {
        let cleanInput = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanInput.isEmpty else {
            present(LinguaFlowError.emptyInput)
            return
        }
        guard let apiKey = settings.apiKey() else {
            present(LinguaFlowError.missingAPIKey)
            return
        }

        isProcessing = true
        errorMessage = nil
        transientMessage = nil
        defer { isProcessing = false }

        do {
            let client = OpenAIClient(apiKey: apiKey, model: settings.model)
            result = try await client.transform(
                text: cleanInput,
                action: action,
                targetLanguage: settings.targetLanguage,
                tone: settings.tone
            )
        } catch {
            present(error)
        }
    }

    func copyResult() {
        guard let output = result?.result else { return }
        selectionService.copyToClipboard(output)
        showTransient("결과를 클립보드에 복사했습니다.")
    }

    func replaceOriginalSelection() async {
        guard let output = result?.result else { return }
        do {
            try await selectionService.replaceSelection(with: output, context: selectionContext)
            showTransient("원래 선택 영역을 교체했습니다.")
        } catch {
            present(error)
        }
    }

    func useResultAsInput() {
        guard let output = result?.result else { return }
        inputText = output
        result = nil
        selectionContext = nil
        sourceAppName = nil
    }

    func clear() {
        inputText = ""
        result = nil
        selectionContext = nil
        sourceAppName = nil
        errorMessage = nil
        transientMessage = nil
    }

    private func present(_ error: Error) {
        if let localized = error as? LocalizedError, let description = localized.errorDescription {
            errorMessage = description
        } else {
            errorMessage = error.localizedDescription
        }
    }

    private func showTransient(_ message: String) {
        transientMessage = message
        Task {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            if transientMessage == message { transientMessage = nil }
        }
    }
}
