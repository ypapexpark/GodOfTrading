import UIKit

@MainActor
final class KeyboardViewController: UIInputViewController {
    private let engine = OnDeviceTextEngine()
    private var targetLanguage: TargetLanguage = .korean
    private var isWorking = false

    private let rootStack = UIStackView()
    private let toolbar = UIStackView()
    private let statusLabel = UILabel()
    private let targetButton = UIButton(type: .system)
    private let translateButton = UIButton(type: .system)
    private let proofreadButton = UIButton(type: .system)
    private let rewriteButton = UIButton(type: .system)
    private let nextKeyboardButton = UIButton(type: .system)

    private let rows = [
        ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
        ["a", "s", "d", "f", "g", "h", "j", "k", "l"],
        ["z", "x", "c", "v", "b", "n", "m"]
    ]

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor.systemGray5
        buildKeyboard()
        refreshTargetButton()
        setStatus("문장을 선택하거나 커서 뒤 문장을 처리하세요.")
    }

    override func viewWillLayoutSubviews() {
        super.viewWillLayoutSubviews()
        nextKeyboardButton.isHidden = !needsInputModeSwitchKey
    }

    private func buildKeyboard() {
        rootStack.axis = .vertical
        rootStack.spacing = 7
        rootStack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(rootStack)

        NSLayoutConstraint.activate([
            rootStack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 5),
            rootStack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -5),
            rootStack.topAnchor.constraint(equalTo: view.topAnchor, constant: 7),
            rootStack.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -7),
            view.heightAnchor.constraint(greaterThanOrEqualToConstant: 292)
        ])

        configureToolbar()
        rootStack.addArrangedSubview(toolbar)

        statusLabel.font = .systemFont(ofSize: 11, weight: .medium)
        statusLabel.textColor = .secondaryLabel
        statusLabel.textAlignment = .center
        statusLabel.numberOfLines = 1
        rootStack.addArrangedSubview(statusLabel)

        for (rowIndex, letters) in rows.enumerated() {
            let row = makeRow()
            if rowIndex > 0 {
                row.layoutMargins = UIEdgeInsets(top: 0, left: CGFloat(rowIndex * 11), bottom: 0, right: CGFloat(rowIndex * 11))
                row.isLayoutMarginsRelativeArrangement = true
            }
            letters.forEach { letter in
                row.addArrangedSubview(makeKey(title: letter, action: #selector(insertCharacter(_:))))
            }
            rootStack.addArrangedSubview(row)
        }

        let bottomRow = makeRow()
        nextKeyboardButton.setTitle("🌐", for: .normal)
        styleKey(nextKeyboardButton, width: 50)
        nextKeyboardButton.addTarget(self, action: #selector(handleInputModeList(from:with:)), for: .allTouchEvents)
        bottomRow.addArrangedSubview(nextKeyboardButton)

        let space = makeKey(title: "space", action: #selector(insertSpace))
        space.widthAnchor.constraint(greaterThanOrEqualToConstant: 155).isActive = true
        bottomRow.addArrangedSubview(space)
        bottomRow.addArrangedSubview(makeKey(title: "return", action: #selector(insertReturn), width: 72))
        bottomRow.addArrangedSubview(makeKey(title: "⌫", action: #selector(deleteCharacter), width: 48))
        rootStack.addArrangedSubview(bottomRow)
    }

    private func configureToolbar() {
        toolbar.axis = .horizontal
        toolbar.spacing = 5
        toolbar.distribution = .fillEqually

        configureActionButton(targetButton, title: "", selector: #selector(cycleTargetLanguage), tint: .systemIndigo)
        configureActionButton(translateButton, title: "번역", selector: #selector(translate), tint: .systemBlue)
        configureActionButton(proofreadButton, title: "교정", selector: #selector(proofread), tint: .systemGreen)
        configureActionButton(rewriteButton, title: "다듬기", selector: #selector(rewrite), tint: .systemOrange)

        [targetButton, translateButton, proofreadButton, rewriteButton].forEach(toolbar.addArrangedSubview)
        toolbar.heightAnchor.constraint(equalToConstant: 38).isActive = true
    }

    private func configureActionButton(_ button: UIButton, title: String, selector: Selector, tint: UIColor) {
        button.setTitle(title, for: .normal)
        button.titleLabel?.font = .systemFont(ofSize: 13, weight: .semibold)
        button.tintColor = tint
        button.backgroundColor = .systemBackground
        button.layer.cornerRadius = 9
        button.addTarget(self, action: selector, for: .touchUpInside)
    }

    private func makeRow() -> UIStackView {
        let row = UIStackView()
        row.axis = .horizontal
        row.spacing = 4
        row.distribution = .fillEqually
        row.heightAnchor.constraint(equalToConstant: 40).isActive = true
        return row
    }

    private func makeKey(title: String, action: Selector, width: CGFloat? = nil) -> UIButton {
        let button = UIButton(type: .system)
        button.setTitle(title, for: .normal)
        button.addTarget(self, action: action, for: .touchUpInside)
        styleKey(button, width: width)
        return button
    }

    private func styleKey(_ button: UIButton, width: CGFloat? = nil) {
        button.backgroundColor = .systemBackground
        button.setTitleColor(.label, for: .normal)
        button.titleLabel?.font = .systemFont(ofSize: 17)
        button.layer.cornerRadius = 6
        if let width {
            button.widthAnchor.constraint(equalToConstant: width).isActive = true
        }
    }

    @objc private func insertCharacter(_ sender: UIButton) {
        guard let text = sender.currentTitle else { return }
        textDocumentProxy.insertText(text)
    }

    @objc private func insertSpace() {
        textDocumentProxy.insertText(" ")
    }

    @objc private func insertReturn() {
        textDocumentProxy.insertText("\n")
    }

    @objc private func deleteCharacter() {
        textDocumentProxy.deleteBackward()
    }

    @objc private func cycleTargetLanguage() {
        targetLanguage = targetLanguage.next()
        refreshTargetButton()
        setStatus("번역 결과: \(targetLanguage.title)")
    }

    @objc private func translate() {
        run(.translate)
    }

    @objc private func proofread() {
        run(.proofread)
    }

    @objc private func rewrite() {
        run(.rewrite)
    }

    private func run(_ action: TransformAction) {
        guard !isWorking else { return }
        guard !isSecureTextEntry else {
            setStatus("보안 입력창에서는 문장을 처리하지 않습니다.")
            return
        }
        guard let capture = SentenceExtractor.capture(
            selectedText: textDocumentProxy.selectedText,
            contextBeforeInput: textDocumentProxy.documentContextBeforeInput
        ) else {
            setStatus(LinguaFlowError.emptyText.localizedDescription)
            return
        }

        isWorking = true
        updateActionButtons(enabled: false)
        setStatus("\(action.title) 중…")

        Task { [weak self] in
            guard let self else { return }
            do {
                let result = try await engine.transform(
                    capture.text,
                    action: action,
                    targetLanguage: targetLanguage
                )
                replace(capture, with: result)
                setStatus("\(action.title) 완료")
            } catch {
                setStatus(error.localizedDescription)
            }
            isWorking = false
            updateActionButtons(enabled: true)
        }
    }

    private var isSecureTextEntry: Bool {
        let traits: UITextInputTraits = textDocumentProxy
        return traits.isSecureTextEntry == true || traits.textContentType == .password || traits.textContentType == .newPassword
    }

    private func replace(_ capture: CapturedText, with result: String) {
        if !capture.replacesSelection {
            for _ in 0..<capture.deleteCount {
                textDocumentProxy.deleteBackward()
            }
        }
        textDocumentProxy.insertText(result)
    }

    private func refreshTargetButton() {
        targetButton.setTitle("→ \(targetLanguage.shortTitle)", for: .normal)
    }

    private func setStatus(_ text: String) {
        statusLabel.text = text
    }

    private func updateActionButtons(enabled: Bool) {
        [targetButton, translateButton, proofreadButton, rewriteButton].forEach {
            $0.isEnabled = enabled
            $0.alpha = enabled ? 1 : 0.5
        }
    }
}
