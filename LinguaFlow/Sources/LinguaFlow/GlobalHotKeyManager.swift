import Carbon.HIToolbox
import Foundation

@MainActor
final class GlobalHotKeyManager {
    private static let signature: OSType = 0x4C464C57 // "LFLW"

    private var handlerRef: EventHandlerRef?
    private var hotKeyRefs: [EventHotKeyRef?] = []
    private var actionHandler: ((WritingAction) -> Void)?

    func start(actionHandler: @escaping (WritingAction) -> Void) {
        guard handlerRef == nil else { return }
        self.actionHandler = actionHandler

        var eventSpec = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
        let userData = Unmanaged.passUnretained(self).toOpaque()
        InstallEventHandler(
            GetApplicationEventTarget(),
            { _, event, userData -> OSStatus in
                guard let event, let userData else { return OSStatus(eventNotHandledErr) }
                var hotKeyID = EventHotKeyID()
                let status = GetEventParameter(
                    event,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    MemoryLayout<EventHotKeyID>.size,
                    nil,
                    &hotKeyID
                )
                guard status == noErr else { return status }
                let manager = Unmanaged<GlobalHotKeyManager>
                    .fromOpaque(userData)
                    .takeUnretainedValue()
                manager.handle(hotKeyID: hotKeyID)
                return noErr
            },
            1,
            &eventSpec,
            userData,
            &handlerRef
        )

        register(action: .translate, virtualKey: UInt32(kVK_ANSI_T))
        register(action: .correct, virtualKey: UInt32(kVK_ANSI_G))
        register(action: .rewrite, virtualKey: UInt32(kVK_ANSI_R))
    }

    private func register(action: WritingAction, virtualKey: UInt32) {
        var hotKeyRef: EventHotKeyRef?
        let hotKeyID = EventHotKeyID(signature: Self.signature, id: action.hotKeyID)
        let modifiers = UInt32(controlKey | optionKey)
        let status = RegisterEventHotKey(
            virtualKey,
            modifiers,
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &hotKeyRef
        )
        if status == noErr {
            hotKeyRefs.append(hotKeyRef)
        }
    }

    private func handle(hotKeyID: EventHotKeyID) {
        guard hotKeyID.signature == Self.signature,
              let action = WritingAction.allCases.first(where: { $0.hotKeyID == hotKeyID.id }) else {
            return
        }
        actionHandler?(action)
    }

    deinit {
        for reference in hotKeyRefs {
            if let reference { UnregisterEventHotKey(reference) }
        }
        if let handlerRef { RemoveEventHandler(handlerRef) }
    }
}
