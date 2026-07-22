import FoundationModels
import SwiftUI
import Translation
import UIKit

struct ContentView: View {
    @State private var sourceLanguage: TargetLanguage = .english
    @State private var targetLanguage: TargetLanguage = .korean
    @State private var translationConfiguration: TranslationSession.Configuration?
    @State private var preparationStatus = "준비할 언어를 선택해 주세요."
    @State private var isPreparing = false
    @State private var testText = "I have a meeting tomorrow."
    @State private var testStatus = "키보드를 켜기 전 앱에서 먼저 시험할 수 있습니다."
    @State private var isTransforming = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    hero
                    appTest
                    keyboardSetup
                    modelSetup
                    privacyCard
                    supportCard
                }
                .padding()
            }
            .background(Color(uiColor: .systemGroupedBackground))
            .navigationTitle("LinguaFlow")
        }
        .translationTask(translationConfiguration) { session in
            do {
                nonisolated(unsafe) let translationSession = session
                try await translationSession.prepareTranslation()
                preparationStatus = "\(sourceLanguage.title) → \(targetLanguage.title) 번역 모델 준비 완료"
            } catch {
                preparationStatus = "준비 실패: \(error.localizedDescription)"
            }
            isPreparing = false
        }
    }

    private var appTest: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("앱에서 먼저 시험", systemImage: "text.viewfinder")
                .font(.title3.bold())
            Text("키보드 확장을 켜기 전에 번역·교정 엔진과 기기 지원 상태를 확인하세요.")
                .font(.subheadline)

            TextEditor(text: $testText)
                .frame(minHeight: 100)
                .padding(6)
                .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12))

            HStack(spacing: 8) {
                testActionButton(.translate)
                testActionButton(.proofread)
                testActionButton(.rewrite)
            }

            Text(testStatus)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .cardStyle()
    }

    private func testActionButton(_ action: TransformAction) -> some View {
        Button(action.title) {
            runAppTransform(action)
        }
        .buttonStyle(.bordered)
        .disabled(isTransforming)
    }

    private func runAppTransform(_ action: TransformAction) {
        guard !isTransforming else { return }
        guard !testText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            testStatus = LinguaFlowError.emptyText.localizedDescription
            return
        }

        isTransforming = true
        testStatus = "(action.title) 중…"
        let input = testText
        let target = targetLanguage

        Task { @MainActor in
            do {
                let result = try await OnDeviceTextEngine().transform(
                    input,
                    action: action,
                    targetLanguage: target
                )
                testText = result
                testStatus = "(action.title) 완료"
            } catch {
                testStatus = error.localizedDescription
            }
            isTransforming = false
        }
    }

    private var hero: some View {
        VStack(alignment: .leading, spacing: 10) {
            Image(systemName: "character.cursor.ibeam")
                .font(.system(size: 38, weight: .semibold))
                .foregroundStyle(.indigo)
            Text("앱을 바꾸지 말고,\n쓰던 입력창에서 바로.")
                .font(.largeTitle.bold())
            Text("LinguaFlow 키보드에서 문장을 번역·교정·다듬고 같은 입력칸에 바로 넣습니다.")
                .foregroundStyle(.secondary)
        }
        .cardStyle()
    }

    private var keyboardSetup: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("1. 키보드 켜기", systemImage: "keyboard")
                .font(.title3.bold())
            Text("설정 → 일반 → 키보드 → 키보드 → 새로운 키보드 추가 → LinguaFlow")
                .font(.subheadline)
            Button {
                UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!)
            } label: {
                Label("설정 열기", systemImage: "gearshape")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            Text("전체 접근 허용은 켜지 않아도 됩니다. 입력창에서 🌐 키를 길게 눌러 LinguaFlow로 전환하세요.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .cardStyle()
    }

    private var modelSetup: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("2. 번역 모델 준비", systemImage: "arrow.trianglehead.2.clockwise.rotate.90")
                .font(.title3.bold())

            Picker("원문", selection: $sourceLanguage) {
                ForEach(TargetLanguage.allCases) { language in
                    Text(language.title).tag(language)
                }
            }
            Picker("번역 결과", selection: $targetLanguage) {
                ForEach(TargetLanguage.allCases) { language in
                    Text(language.title).tag(language)
                }
            }

            Button {
                guard sourceLanguage != targetLanguage else {
                    preparationStatus = "원문과 번역 결과 언어를 다르게 선택해 주세요."
                    return
                }
                isPreparing = true
                preparationStatus = "Apple 번역 모델을 확인하는 중…"
                translationConfiguration = TranslationSession.Configuration(
                    source: sourceLanguage.localeLanguage,
                    target: targetLanguage.localeLanguage
                )
                translationConfiguration?.invalidate()
            } label: {
                if isPreparing {
                    ProgressView().frame(maxWidth: .infinity)
                } else {
                    Text("선택한 번역 모델 준비").frame(maxWidth: .infinity)
                }
            }
            .buttonStyle(.borderedProminent)
            .disabled(isPreparing)

            Text(preparationStatus)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .cardStyle()
    }

    private var privacyCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("비용·개인정보", systemImage: "lock.shield")
                .font(.title3.bold())
            Text("이 MVP는 OpenAI 같은 유료 API 키를 사용하지 않습니다. 번역과 문장 처리는 Apple이 제공하는 기기 내 모델로 실행하며, 키보드는 네트워크 전체 접근 권한을 요청하지 않습니다.")
                .font(.subheadline)
            Text("번역 모델을 처음 준비할 때는 시스템 다운로드가 필요할 수 있습니다.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .cardStyle()
    }

    private var supportCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("기기 지원 상태", systemImage: "cpu")
                .font(.title3.bold())
            Text(appleIntelligenceStatus)
                .font(.subheadline)
            Text("Apple Intelligence가 없는 기기에서도 Apple 온디바이스 번역은 별도로 사용할 수 있습니다.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .cardStyle()
    }

    private var appleIntelligenceStatus: String {
        switch SystemLanguageModel.default.availability {
        case .available:
            "교정·다듬기 사용 가능"
        case .unavailable(.deviceNotEligible):
            "이 기기는 Apple Intelligence 교정·다듬기를 지원하지 않음"
        case .unavailable(.appleIntelligenceNotEnabled):
            "설정에서 Apple Intelligence를 켜야 교정·다듬기 사용 가능"
        case .unavailable(.modelNotReady):
            "Apple Intelligence 모델 준비 중"
        @unknown default:
            "Apple Intelligence 상태를 확인할 수 없음"
        }
    }

}

private extension View {
    func cardStyle() -> some View {
        self
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(18)
            .background(.background, in: RoundedRectangle(cornerRadius: 20))
    }
}
