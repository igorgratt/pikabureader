# Android-приложение (M11)

PikaBuReader — Progressive Web App (PWA), который устанавливается на Android как нативное приложение.

## Установка через PWA (рекомендуется)

Откройте PikaBuReader в Chrome на Android:

1. Меню Chrome (⋮) → «Добавить на главный экран»
2. Приложение появится на рабочем столе
3. Работает офлайн (статика закэширована через Service Worker)

## Сборка APK (TWA / Bubblewrap)

Для Google Play или установки через APK-файл:

### Требования

- Node.js 18+
- Java JDK 17+ (`java -version`)
- Android SDK с `platforms;android-34` и `build-tools`

```bash
# Установить Bubblewrap
npm install -g bubblewrap

# Собрать APK
powershell -ExecutionPolicy Bypass -File tools\build_android.ps1 -HostUrl "https://your-server.example.com"
```

APK появится в `dist/pikabureader-android.apk`.

### Установка APK

```bash
# через ADB (Android Debug Bridge)
adb install dist\pikabureader-android.apk
```

Или скопируйте файл на телефон и откройте → «Установить».
На Android 8+ включите «Установка из неизвестных источников» для браузера.

### Asset Links (для верификации в Google Play)

Google Play требует подтверждения связи между APK и веб-приложением.
Для этого нужен файл `/.well-known/assetlinks.json` на сервере:

```json
[
  {
    "relation": ["delegate_permission/common.handle_all_urls"],
    "target": {
      "namespace": "android_app",
      "packageName": "app.pikabureader",
      "sha256_cert_fingerprints": ["XX:XX:..."]
    }
  }
]
```

**SHA-256 отпечаток сертификата** получается из подписи APK:

```bash
# отладочная подпись (keys/debug.keystore)
keytool -list -v -keystore ~/.android/debug.keystore

# релизная подпись — из вашего keystore
keytool -list -v -keystore your-release.keystore
```

Сгенерируйте `assetlinks.json` и положите в `docs/assetlinks.json`
(не в `static/` — он должен отдаваться с корня домена).

Для локальной разработки (`localhost`) assetlinks не требуется.

### Публикация в Google Play

1. Зарегистрируйтесь как разработчик (one-time $25)
2. Создайте приложение в Google Play Console
3. Загрузите подписанный APK/AAB
4. Заполните описание, скриншоты, возрастной рейтинг
5. Отправьте на проверку (обычно 1–3 дня)

Для релиза используйте подписанный APK:

```bash
# сгенерировать keystore (один раз)
keytool -genkey -v -keystore pikabu-release.keystore -alias pikabu -keyalg RSA -keysize 2048 -validity 10000

# подписать APK
jarsigner -verbose -sigalg SHA256withRSA -digestalg SHA-256 \
  -keystore pikabu-release.keystore dist\pikabureader-android.apk pikabu
```

### Структура файлов

```
twa-manifest.json          # конфигурация TWA для Bubblewrap
tools/build_android.ps1    # скрипт сборки APK
static/manifest.webmanifest  # PWA-манифест
static/icons/icon-192.png  # иконка 192×192
static/icons/icon-512.png  # иконка 512×512 (maskable)
```
