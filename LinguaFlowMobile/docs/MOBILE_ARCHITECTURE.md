# LinguaFlow 모바일 설계

## 1. 제품 원칙

핵심 성공 기준은 “번역 앱을 열지 않는다”이다. 사용자는 현재 입력창을 떠나지 않고 2번 이내의 동작으로 결과를 적용해야 한다.

1. 선택한 문장이 있으면 그 문장만 처리한다.
2. 선택 영역이 없으면 커서 바로 앞의 현재 문장을 처리한다.
3. 결과는 별도 클립보드 단계를 거치지 않고 원래 입력창에 치환한다.
4. 기기 내 처리를 기본값으로 하고, 지원되지 않는 기능은 조용히 실패하지 않고 이유를 표시한다.
5. 비밀번호와 보안 입력란은 읽지 않는다.

## 2. 사용자 진입점

### A. 텍스트 선택 메뉴

기존 키보드를 유지하려는 사용자에게 가장 마찰이 적은 경로다.

`문장 길게 누르기 → 선택 → 더보기 → LinguaFlow로 다듬기 → 원문 교체`

Android에서는 `ACTION_PROCESS_TEXT`로 구현되어 있다. 호스트 앱이 편집 가능한 문장을 넘긴 경우 처리 결과를 같은 입력란으로 반환한다. 읽기 전용 화면에서는 복사만 제공한다.

### B. LinguaFlow 키보드 툴바

반복 사용자를 위한 가장 빠른 경로다.

`문장 입력 → 번역/교정/다듬기 → 즉시 치환`

Android에서는 `InputMethodService`와 `InputConnection`을 사용한다. 선택 텍스트가 없으면 최대 1,000자의 커서 앞 문맥에서 마지막 문장을 찾는다.

### C. 앱 안의 시험 화면

키보드 설정, 기본 번역 언어, 엔진 제한을 설명하고 기능을 안전하게 시험하는 온보딩 화면이다. 실제 제품 가치는 A와 B에서 발생한다.

## 3. Android 구현

```text
MainActivity
 ├─ 키보드 활성화/선택
 ├─ 기본 번역 언어
 └─ 기능 시험

ProcessTextActivity
 └─ 선택 문장 → 엔진 → 원문 교체 또는 복사

LinguaFlowImeService
 └─ 선택/현재 문장 → 엔진 → InputConnection 즉시 치환

TextTransformEngine
 ├─ TranslationEngine (ML Kit Language ID + Translation)
 └─ OnDeviceWritingEngine (Gemini Nano Proofreading/Rewriting)
```

엔진 결과는 `Success`, `Progress`, `Unavailable`, `Failure`로 통일했다. UI는 SDK 예외를 직접 해석하지 않고 이 상태만 표시한다. 나중에 유료 AI를 추가할 때도 `TextTransformEngine` 뒤에 Provider를 추가하면 입력 UI를 바꿀 필요가 없다.

## 4. iPhone 설계안

### 배포 단위

- `LinguaFlow` 컨테이너 앱: 온보딩, 기본 언어, 번역 언어 모델 준비, 개인정보 설명
- `LinguaFlowKeyboard` Custom Keyboard Extension: 입력 툴바, 문맥 추출, 결과 치환
- 공유 도메인 모듈: 문장 경계, 동작 타입, 설정 모델

### 키보드 동작

`UIInputViewController.textDocumentProxy`에서 선택 텍스트 또는 커서 주변 문맥을 가져오고, `deleteBackward()`와 `insertText()`로 결과를 교체한다. 네트워크를 쓰지 않는 1차 버전은 `RequestsOpenAccess = false`로 출시해 “전체 접근 허용” 요구를 없애는 방향을 우선한다.

### 무료 기능 후보

1. 번역: Apple `Translation` 프레임워크와 설치된 온디바이스 언어를 사용한다. 언어 모델 다운로드와 권한 UI는 컨테이너 앱에서 미리 처리한다.
2. 교정·다듬기(iOS 26 이상): Apple Intelligence 지원 기기에서 `FoundationModels`의 `LanguageModelSession`을 사용해 결과 텍스트만 생성한다. 호출당 API 요금은 없다.
3. 낮은 사양/이전 OS: `UITextChecker` 기반 철자 교정만 제공하고, 전체 문장 재작성은 미지원 상태로 표시한다.

### iOS 구현 전 필수 검증

Apple의 Custom Keyboard Extension은 호스트 앱과 분리된 샌드박스에서 실행된다. 따라서 첫 iOS 스파이크에서 다음을 실제 기기로 검증한 뒤 확정한다.

- `TranslationSession`과 `FoundationModels`가 키보드 확장에서 App Store 허용 API로 실행되는지
- 설치된 언어 모델만 사용할 때 `RequestsOpenAccess = false`를 유지할 수 있는지
- 호스트 앱별 `selectedText`, `documentContextBeforeInput` 제공 범위와 치환 정확도
- Apple Intelligence 미지원 기기에서의 일관된 폴백

검증 결과 특정 프레임워크가 키보드 확장에서 금지된다면, “전체 접근 허용 + 컨테이너 앱과 App Group IPC”로 우회하지 않고 먼저 텍스트 선택용 Share/Action Extension을 제공한다. 개인정보 신뢰를 제품 편의보다 우선한다.

## 5. 유료 AI 확장 지점

유료 AI는 기본 기능이 아니라 선택형 고급 Provider로 둔다.

- 기본: `ON_DEVICE`
- 선택: `CLOUD_PREMIUM`
- 입력창 UI와 치환 로직은 공통
- 클라우드 사용 전 명시적 동의와 전송 범위 표시
- API 키는 모바일 앱에 직접 내장하지 않고 자체 백엔드에서 관리

이 구조라면 초기에는 비용 0원으로 검증하고, 나중에 번역 품질·복잡한 말투·긴 문서 수요가 확인될 때만 서버 비용을 도입할 수 있다.
