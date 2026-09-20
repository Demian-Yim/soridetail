"""헤이 비전 안드로이드 앱 패키지 생성 — PWABuilder CloudAPK 서비스에 요청해 APK/AAB 를 받는다.

    python scripts/android_package.py https://<배포 주소> [출력 폴더=android/]

받은 zip 에는 서명된 .apk(바로 설치 시험용)·.aab(플레이스토어 제출용)·서명 키(.keystore)·비밀번호·assetlinks.json 이 들어 있다.
서명 키와 비밀번호는 절대 저장소에 넣지 않는다 — 출력 폴더는 .gitignore 에 올라가 있다.
"""
import json
import secrets
import sys
import zipfile
from pathlib import Path

import httpx

CLOUDAPK = "https://pwabuilder-cloudapk.azurewebsites.net/generateAppPackage"   # [추정] PWABuilder 공개 인스턴스
PACKAGE_ID = "kr.flowax.heyvision"


def build_request(host: str, store_password: str, key_password: str) -> dict:
    host = host.rstrip("/")
    return {
        "packageId": PACKAGE_ID,
        "name": "헤이 비전",
        "launcherName": "헤이 비전",
        "host": host,
        "startUrl": "/eye?source=app",
        "webManifestUrl": f"{host}/static/manifest.webmanifest",
        "iconUrl": f"{host}/static/icons/icon-512.png",
        "maskableIconUrl": f"{host}/static/icons/maskable-512.png",
        "themeColor": "#0B0B0B",
        "backgroundColor": "#0B0B0B",
        "appVersion": "1.0.0.0",
        "appVersionCode": 1,
        "display": "standalone",
        "orientation": "portrait",
        "fallbackType": "customtabs",
        "features": {"locationDelegation": {"enabled": False}, "playBilling": {"enabled": False}},
        "isChromeOSOnly": False,
        "includeSourceCode": False,
        "signingMode": "new",
        "signing": {
            "file": None,
            "alias": "heyvision",
            "fullName": "FLOW : AX디자인연구소",
            "organization": "FLOW : AX디자인연구소",
            "organizationalUnit": "Hey Vision",
            "countryCode": "KR",
            "storePassword": store_password,
            "keyPassword": key_password,
        },
    }


def main(host: str, out_dir: str = "android") -> int:
    out = Path(out_dir)
    out.mkdir(exist_ok=True)
    store_password, key_password = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
    body = build_request(host, store_password, key_password)
    print(f"요청: {CLOUDAPK} · host={body['host']} · packageId={PACKAGE_ID}")
    with httpx.Client(timeout=600) as client:
        res = client.post(CLOUDAPK, json=body)
    if res.status_code != 200:
        print(f"실패 {res.status_code}: {res.text[:400]}")
        return 1
    zip_path = out / "heyvision-android.zip"
    zip_path.write_bytes(res.content)
    (out / "SIGNING-PASSWORDS.txt").write_text(
        f"storePassword={store_password}\nkeyPassword={key_password}\nalias=heyvision\n", encoding="utf-8")
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        zf.extractall(out)
    print(f"저장: {zip_path} ({len(res.content) / 1e6:.1f}MB) · 내용물 {len(names)}개")
    for name in names:
        print("  -", name)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "android"))
