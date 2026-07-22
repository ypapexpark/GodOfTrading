# LinguaFlow Mobile

LinguaFlow는 번역이나 문장 교정을 위해 다른 앱으로 이동하고 복사·붙여넣기하는 과정을 없애는 모바일 입력 도구다.

현재 Android MVP가 구현되어 있다. 유료 AI API나 API 키를 사용하지 않으며, 텍스트 처리는 Google/Android가 제공하는 기기 내 기능으로 수행한다.

## 지금 동작하는 것

- Android 텍스트 선택 메뉴: 어느 앱에서든 문장을 선택한 뒤 `LinguaFlow로 다듬기`를 열어 번역·교정·다듬기하고 원문을 교체한다.
- LinguaFlow 키보드: 툴바의 `번역`, `교정`, `다듬기` 버튼으로 선택 문장 또는 커서 바로 앞 문장을 같은 입력칸에서 교체한다.
- 앱 안의 시험 입력창: 설치 직후 기능과 기기 지원 상태를 확인할 수 있다.
- 개인정보 보호: 비밀번호 입력란에서는 문장 처리 버튼이 자동으로 비활성화된다.

## 무료 엔진

| 기능 | Android MVP | 비용 | 제한 |
|---|---|---:|---|
| 언어 감지·번역 | Google ML Kit 온디바이스 번역 | 호출당 비용 없음 | 언어 모델을 최초 한 번 다운로드 |
| 문법·맞춤법 교정 | ML Kit Proofreading + Gemini Nano | 서버/API 비용 없음 | 지원되는 Android 기기에서만 사용 가능 |
| 문장 다듬기 | ML Kit Rewriting + Gemini Nano | 서버/API 비용 없음 | 지원 기기·언어·짧은 문장 제한 |

번역 모델 다운로드에는 인터넷이 필요하지만, 다운로드 후 번역은 기기 안에서 실행된다. 교정·다듬기를 지원하지 않는 기기에서도 번역은 계속 사용할 수 있다.

번역 결과는 Google 번역 기술로 제공된다. 앱의 번역 버튼·결과와 도움말에는 [Google 번역 출처 및 자동 번역 고지](https://developers.google.com/ml-kit/language/translation/translation-terms)를 표시해야 한다.

## 실행 방법

1. Android Studio에서 [`android`](./android) 폴더를 연다.
2. `app` 실행 구성을 실제 Android 기기 또는 에뮬레이터에 설치한다.
3. LinguaFlow 앱의 `키보드 활성화`를 누르고 시스템 설정에서 LinguaFlow 키보드를 켠다.
4. 입력창에서 지구본 키를 눌러 LinguaFlow로 전환한다.

터미널 빌드는 `android` 폴더에서 다음 명령으로 확인할 수 있다.

```bash
./gradlew testDebugUnitTest assembleDebug lintDebug
```

디버그 APK는 `android/app/build/outputs/apk/debug/app-debug.apk`에 생성된다.

## Android MVP의 의도적인 범위

- V1 키보드는 영문 QWERTY 입력을 제공한다. 한글을 직접 조합해 입력하는 기능은 아직 없다.
- 한글을 주로 입력할 때는 기존 한글 키보드로 작성하고 문장을 선택해 `LinguaFlow로 다듬기`를 쓰거나, 지구본 키로 잠시 LinguaFlow로 전환해 현재 문장을 처리하면 된다.
- Android는 다른 키보드(Gboard, 삼성 키보드)의 내부 툴바에 서드파티 버튼을 추가하는 API를 제공하지 않는다. 그래서 LinguaFlow 자체 키보드와 텍스트 선택 메뉴를 함께 제공한다.
- 일부 금융·보안 앱은 제3자 키보드 또는 텍스트 선택 작업을 막을 수 있다.
- ML Kit 번역은 일상적인 짧은 문장에 적합하다. 중요한 계약·의료·법률 문장은 사람이 검토해야 한다.

## 다음 단계

- iPhone용 Custom Keyboard Extension과 온디바이스 Translation 연동
- Android 한글 조합 입력기 또는 `툴바 전용 + 기존 키보드 빠른 복귀` UX 고도화
- 지원 기기에서 말투 선택(친근하게, 비즈니스, 짧게)
- 향후 선택형 유료 AI Provider를 추가하되, 기본값은 계속 온디바이스로 유지

상세 구조와 iPhone 설계는 [`MOBILE_ARCHITECTURE.md`](./docs/MOBILE_ARCHITECTURE.md)에 정리되어 있다.

Android 친구에게 전달할 설치·검수 절차는 [`ANDROID_FRIEND_TEST_GUIDE_KO.md`](./docs/ANDROID_FRIEND_TEST_GUIDE_KO.md)를 그대로 보내면 된다.
