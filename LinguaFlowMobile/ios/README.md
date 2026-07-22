# LinguaFlow iPhone MVP

LinguaFlow iPhone 버전은 본 앱과 Custom Keyboard Extension으로 구성된다. iOS 26 이상의 Apple 기본 프레임워크만 사용하며, 유료 API 키는 필요 없다.

## 기능

- 앱 안의 시험 입력창에서 번역·교정·다듬기 엔진 확인
- 키보드 입력과 지우기, 공백, 줄바꿈, 다음 키보드 전환
- 선택한 문장 또는 커서 바로 앞 문장 번역
- Apple Intelligence 지원 기기의 문법 교정과 자연스러운 문장 다듬기
- 한국어·영어·일본어 번역 대상 전환
- 보안 입력창 처리 차단
- 키보드의 `전체 접근 허용` 불필요
- 본 앱에서 필요한 Apple 번역 모델 준비

## 요구 사항

- macOS와 Xcode 26 이상
- iOS 26 이상인 iPhone
- 교정·다듬기: Apple Intelligence 지원 기기 및 기능 활성화
- 번역: Apple Translation에서 지원하는 언어 조합

## 프로젝트 열기

`LinguaFlowIOS.xcodeproj`를 Xcode로 연다. 프로젝트를 다시 생성해야 한다면 다음 명령을 실행한다.

```bash
ruby scripts/generate_xcodeproj.rb
```

실제 iPhone에 처음 설치할 때는 Xcode의 Signing & Capabilities에서 본인의 Apple ID Personal Team을 선택하고, 앱과 키보드 Bundle Identifier가 계정 안에서 고유하도록 바꾼다.

자세한 설치·사용법은 [`../docs/IPHONE_INSTALL_AND_TEST_GUIDE_KO.md`](../docs/IPHONE_INSTALL_AND_TEST_GUIDE_KO.md)를 참고한다.
