# LinguaFlow iPhone 설치·테스트 안내

현재 저장소의 iPhone MVP는 `iOS 26 이상`을 대상으로 합니다. iPhone에서는 Android APK처럼 파일 하나를 받아 설치할 수 없고, 아래 두 방법 중 하나로 설치해야 합니다.

## 가장 빠른 방법: 내 iPhone에 Xcode로 설치

필요한 것:

- macOS
- Xcode 26 이상
- iOS 26 이상인 실제 iPhone
- Apple 계정(무료 계정으로 개인 기기 개발 테스트 가능)
- iPhone과 Mac을 연결할 USB 케이블

1. `LinguaFlowMobile/ios/LinguaFlowIOS.xcodeproj`를 Xcode에서 엽니다.
2. iPhone을 Mac에 연결하고 잠금을 해제한 뒤 “이 컴퓨터를 신뢰”를 선택합니다.
3. Xcode에서 앱 타깃과 `LinguaFlowKeyboard` 타깃의 **Signing & Capabilities → Team**에 본인의 Apple 계정을 선택합니다.
4. Bundle Identifier가 이미 사용 중이면 두 타깃의 식별자를 본인 계정에서 고유한 값으로 바꿉니다.
5. 실행 기기로 연결한 iPhone을 선택하고 Run(▶)을 누릅니다.
6. iPhone에서 `설정 → 개인정보 보호 및 보안 → 개발자 모드`를 켜고 재시동합니다(처음 한 번만).
7. 설치가 끝나면 `설정 → 일반 → 키보드 → 키보드 → 새로운 키보드 추가 → LinguaFlow`를 선택합니다.
8. 메모나 메시지 입력창에서 지구본 키를 길게 눌러 LinguaFlow로 전환합니다.

무료 Apple 계정으로 설치한 앱은 개발용 서명이 짧은 주기로 만료되므로, 만료되면 Xcode에서 다시 Run해야 합니다. 이 방식은 본인 기기 확인용이며 친구에게 배포하는 방법은 아닙니다.

## 친구에게 배포: TestFlight

친구의 iPhone에 설치하려면 Apple Developer Program 가입(연간 유료) 후 App Store Connect에서 TestFlight 빌드를 배포하는 것이 정식 경로입니다.

1. Xcode에서 Archive를 만들고 App Store Connect에 업로드합니다.
2. App Store Connect → TestFlight에서 친구의 Apple 계정을 테스터로 초대하거나 공개 링크를 만듭니다.
3. 친구는 App Store에서 **TestFlight**를 설치하고 초대 링크를 엽니다.
4. TestFlight에서 LinguaFlow를 설치한 뒤 위의 키보드 추가 절차를 진행합니다.
5. 번역·교정 버튼을 눌러 결과가 같은 입력창에 들어오는지 확인합니다.

TestFlight 빌드는 일정 기간 후 만료될 수 있으므로 만료 전에 새 빌드를 올립니다. 현재 프로젝트에는 서명된 `.ipa`나 TestFlight 빌드가 포함되어 있지 않습니다.

## 기능 범위와 제한

- 번역은 Apple `Translation`의 기기 내 언어 모델을 사용합니다. 첫 사용 전 LinguaFlow 앱에서 `번역 모델 준비`를 눌러 언어쌍을 다운로드해야 할 수 있습니다.
- 교정·다듬기는 Apple Intelligence 지원 기기와 활성화 상태에서만 동작합니다. 지원하지 않는 기기에서는 번역만 계속 사용할 수 있습니다.
- 현재 키보드는 영문 QWERTY 중심의 MVP입니다. 기존 한글 키보드로 작성한 뒤 지구본 키로 LinguaFlow를 잠시 선택해 문장을 처리하는 방식으로 테스트하세요.
- 비밀번호·전화번호 같은 보안/특수 입력란에서 LinguaFlow가 표시되지 않거나 문장을 처리하지 않는 것은 정상입니다.
- `전체 접근 허용(Allow Full Access)`은 현재 오프라인 MVP에서 켜지 않아도 됩니다. 향후 클라우드 AI를 추가할 때만 별도 동의와 함께 검토합니다.
- Apple의 Custom Keyboard Extension은 호스트 앱·기기·OS에 따라 텍스트 문맥 제공 범위가 다릅니다. 선택 문장 또는 커서 앞 문장 처리 결과를 실제 iPhone의 메모, 메시지, 브라우저에서 각각 확인해야 합니다.
- Apple 문서는 `Translation`과 `FoundationModels`의 Custom Keyboard Extension 실행을 별도로 보장하지 않습니다. 따라서 현재 코드는 “실기기 POC 단계”이며, 번역 모델을 앱에서 먼저 준비한 뒤 키보드에서 번역 버튼을 실제로 눌러 지원 여부를 확인합니다. 키보드에서 직접 실행되지 않는 경우에는 앱의 `앱에서 먼저 시험` 화면으로 엔진을 별도 검증할 수 있습니다.

## 친구에게 보낼 테스트 문구

> TestFlight를 설치하고 LinguaFlow 초대 링크로 앱을 설치해줘. 설치 후 `설정 → 일반 → 키보드 → 키보드 → 새로운 키보드 추가`에서 LinguaFlow를 켜고, 메모 입력창에서 지구본 키를 길게 눌러 LinguaFlow로 바꿔줘. `I have a meeting tomorrow.`를 입력한 뒤 키보드의 번역 버튼을 눌러 같은 입력창에서 한국어로 바뀌는지 확인해줘. 교정·다듬기도 눌러보고, 안 되면 iPhone 모델·iOS 버전·사용한 앱·오류 화면을 보내줘. 비밀번호 입력창은 테스트하지 않아도 돼.

## 공식 참고

- [Apple: Creating a custom keyboard](https://developer.apple.com/documentation/uikit/creating-a-custom-keyboard)
- [Apple: Translation framework](https://developer.apple.com/documentation/translation)
- [Apple: TestFlight](https://developer.apple.com/testflight/)
- [Apple Developer Program 멤버십 비교](https://developer.apple.com/support/compare-memberships/)
