"""共通ヘルパー。接続先は環境変数で上書きできる。"""
import os
import sys
import time

import httpx



def _load_dotenv():
    """リポジトリ直下の .env を読み込む（PowerShellでexportしなくて済むように）。既存の環境変数が優先。"""
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if not os.path.exists(env):
        return
    for line in open(env, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

PROXY = os.environ.get("LITELLM_URL", f"http://localhost:{os.environ.get('LITELLM_PORT', '4000')}")
MASTER = os.environ.get("LITELLM_MASTER_KEY", "")
if not MASTER or "REPLACE_ME" in MASTER:
    sys.exit("LITELLM_MASTER_KEY が未設定です。scripts/init_env.sh で .env を作るか、.env に長いランダムな値を設定してください。")
PRIMARY = os.environ.get("MOCK_PRIMARY_URL", f"http://localhost:{os.environ.get('MOCK_PRIMARY_PORT', '19001')}")
BACKUP = os.environ.get("MOCK_BACKUP_URL", f"http://localhost:{os.environ.get('MOCK_BACKUP_PORT', '19002')}")

# 1: DBなしで動かしている場合。仮想キーが使えないので、マスターキーで代用する
NO_DB = os.environ.get("LAB_NO_DB") == "1"

COST_PER_REQUEST = 100 * 0.00001 + 100 * 0.00003  # litellm_config.yamlと一致させる


def admin_headers():
    return {"Authorization": f"Bearer {MASTER}"}


def wait_ready(timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(f"{PROXY}/health/readiness", timeout=3).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    sys.exit("proxy not ready")


def mock(url, path, **json):
    r = httpx.post(f"{url}{path}", json=json, timeout=5)
    r.raise_for_status()
    return r.json()


def mock_stats(url):
    return httpx.get(f"{url}/admin/stats", timeout=5).json()


def reset_mocks():
    mock(PRIMARY, "/admin/reset")
    mock(BACKUP, "/admin/reset")


def gen_key(**body):
    r = httpx.post(f"{PROXY}/key/generate", headers=admin_headers(), json=body, timeout=30)
    r.raise_for_status()
    return r.json()["key"]


def lab_key(**body):
    """仮想キーを発行する。DBなしモードではマスターキーを返す。"""
    return MASTER if NO_DB else gen_key(**body)


def key_info(key):
    r = httpx.get(f"{PROXY}/key/info", params={"key": key}, headers=admin_headers(), timeout=30)
    r.raise_for_status()
    return r.json()["info"]


def chat(key, model, client=None, timeout=60, content="ping"):
    c = client or httpx
    return c.post(
        f"{PROXY}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": [{"role": "user", "content": content}]},
        timeout=timeout,
    )


class Checks:
    """合否を集計して、最後に終了コードへ反映する。"""

    def __init__(self):
        self.results = []

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok)))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))

    def note(self, text):
        """合否に数えない補足（検証不能・観測値など）。"""
        print(f"  [INFO] {text}")

    def finish(self):
        failed = [n for n, ok in self.results if not ok]
        print(f"\n{len(self.results) - len(failed)}/{len(self.results)} passed")
        sys.exit(1 if failed else 0)
