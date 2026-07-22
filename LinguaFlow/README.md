# LinguaFlow

macOS의 어느 앱에서든 선택한 문장을 번역하거나 교정하고, 자연스럽게 다듬는 메뉴바 앱입니다.

## 현재 MVP

- 전역 단축키로 선택 문장 가져오기
  - 번역: `⌃⌥T`
  - 문법 교정: `⌃⌥G`
  - 자연스럽게 쓰기: `⌃⌥R`
- 번역 언어와 문장 톤 선택
- OpenAI Responses API의 strict JSON schema 응답
- 결과 미리보기, 수정 내역, 복사, 원문 교체
- API 키를 macOS Keychain에만 저장
- 선택한 원문을 앱 로그에 저장하지 않음
- Accessibility 직접 교체가 안 되는 편집기는 클립보드 붙여넣기로 폴백

## 실행

Xcode 26 / Swift 6 이상의 macOS 환경에서:

```bash
cd LinguaFlow
swift run LinguaFlow
```

앱 번들로 빌드하려면:

```bash
cd LinguaFlow
zsh scripts/build-app.sh
open build/LinguaFlow.app
```

## 최초 설정

1. 메뉴바의 LinguaFlow 아이콘에서 `설정…`을 엽니다.
2. OpenAI API 키를 저장합니다. 키는 macOS Keychain에 저장됩니다.
3. `권한 요청`을 누릅니다.
4. 시스템 설정 → 개인정보 보호 및 보안 → 손쉬운 사용에서 LinguaFlow를 허용합니다.
5. 다른 앱에서 문장을 선택한 뒤 단축키를 누릅니다.

개발 중에는 `OPENAI_API_KEY` 환경 변수도 사용할 수 있습니다. 환경 변수가 있으면 Keychain 값보다 우선합니다.

## 개인정보 참고

API 요청에는 `store: false`를 사용합니다. LinguaFlow는 원문이나 결과를 자체 데이터베이스 또는 로그에 저장하지 않습니다. OpenAI 측 API 데이터 보존 정책은 별도로 적용되므로 배포 전 개인정보 처리방침에 고지해야 합니다.

## 검증

```bash
swift test
swift build -c release
```
