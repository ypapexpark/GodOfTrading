import AppKit
import ApplicationServices
import Foundation

final class TextSelectionContext {
    let element: AXUIElement
    let applicationPID: pid_t
    let applicationName: String

    init(element: AXUIElement, applicationPID: pid_t, applicationName: String) {
        self.element = element
        self.applicationPID = applicationPID
        self.applicationName = applicationName
    }
}

struct CapturedSelection {
    let text: String
    let context: TextSelectionContext
}

@MainActor
final class TextSelectionService {
    func isAccessibilityTrusted(prompt: Bool = false) -> Bool {
        guard prompt else { return AXIsProcessTrusted() }
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        return AXIsProcessTrustedWithOptions(options)
    }

    func requestAccessibilityPermission() {
        _ = isAccessibilityTrusted(prompt: true)
    }

    func captureSelection() throws -> CapturedSelection {
        guard isAccessibilityTrusted() else {
            requestAccessibilityPermission()
            throw LinguaFlowError.accessibilityPermissionRequired
        }

        let systemWide = AXUIElementCreateSystemWide()
        var focusedValue: CFTypeRef?
        let focusedStatus = AXUIElementCopyAttributeValue(
            systemWide,
            kAXFocusedUIElementAttribute as CFString,
            &focusedValue
        )
        guard focusedStatus == .success, let focusedValue else {
            throw LinguaFlowError.selectionUnavailable("현재 입력창이 손쉬운 사용 API를 지원하지 않습니다.")
        }

        let focusedElement = focusedValue as! AXUIElement
        var selectedValue: CFTypeRef?
        let selectedStatus = AXUIElementCopyAttributeValue(
            focusedElement,
            kAXSelectedTextAttribute as CFString,
            &selectedValue
        )
        guard selectedStatus == .success,
              let selectedText = selectedValue as? String,
              !selectedText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw LinguaFlowError.noSelectedText
        }

        var pid: pid_t = 0
        AXUIElementGetPid(focusedElement, &pid)
        let appName = NSRunningApplication(processIdentifier: pid)?.localizedName ?? "다른 앱"
        return CapturedSelection(
            text: selectedText,
            context: TextSelectionContext(
                element: focusedElement,
                applicationPID: pid,
                applicationName: appName
            )
        )
    }

    func clipboardText() -> String? {
        NSPasteboard.general.string(forType: .string)
    }

    func copyToClipboard(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    func replaceSelection(with text: String, context: TextSelectionContext?) async throws {
        guard let context else {
            copyToClipboard(text)
            throw LinguaFlowError.selectionUnavailable("원래 선택 영역이 없어 결과를 클립보드에 복사했습니다.")
        }

        let status = AXUIElementSetAttributeValue(
            context.element,
            kAXSelectedTextAttribute as CFString,
            text as CFString
        )
        if status == .success { return }

        // Some web and Electron editors expose the selection but do not allow
        // setting kAXSelectedText. Restore the source app and paste instead.
        copyToClipboard(text)
        guard let application = NSRunningApplication(processIdentifier: context.applicationPID) else {
            throw LinguaFlowError.selectionUnavailable("원래 앱을 다시 활성화할 수 없어 결과를 복사했습니다.")
        }
        application.activate()
        try await Task.sleep(nanoseconds: 160_000_000)
        postPasteShortcut()
    }

    private func postPasteShortcut() {
        let source = CGEventSource(stateID: .combinedSessionState)
        let keyCodeV: CGKeyCode = 9
        let keyDown = CGEvent(keyboardEventSource: source, virtualKey: keyCodeV, keyDown: true)
        let keyUp = CGEvent(keyboardEventSource: source, virtualKey: keyCodeV, keyDown: false)
        keyDown?.flags = .maskCommand
        keyUp?.flags = .maskCommand
        keyDown?.post(tap: .cghidEventTap)
        keyUp?.post(tap: .cghidEventTap)
    }
}
