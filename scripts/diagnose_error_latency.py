"""障害時に1リクエストが約2秒かかる原因を、経路ごとに切り分ける。

プライマリを500エラーにした状態で、次の4経路の所要時間を測る。
  A. モックを直接呼ぶ                ... モック自体は即座に500を返すはず（基準）
  B. LiteLLM経由・bench-llm          ... フォールバックなし。エラー処理そのものの時間
  C. LiteLLM経由・main-llm           ... プライマリ失敗→backup-llm へフォールバック
  D. LiteLLM経由・backup-llm         ... 成功経路（基準）

判定の目安
  Bが遅い        → エラー処理そのもの（例外の変換・失敗ログのDB書き込み・SDKの再試行など）が遅い
  Bは速いがCが遅い → フォールバックの経路で待ちが入っている
  どれも速い      → この環境では再現しない
併せて、B で モックに何回到達したかも出す（2回以上ならSDK/ルーターの再試行が入っている）。

補足: 詳しく見るときは .env に LITELLM_LOG=DEBUG を一時的に入れて litellm を作り直し、
  docker compose logs --since 2m litellm でタイムスタンプを確認する（終わったら ERROR に戻す）。
"""
import os
import statistics
import time

import httpx

import lab

N = int(os.environ.get("N", "5"))

lab.wait_ready()
lab.reset_mocks()
key = lab.lab_key(models=["bench-llm", "main-llm", "backup-llm"])
lab.mock(lab.PRIMARY, "/admin/mode", mode="error", status=500)
BODY = {"messages": [{"role": "user", "content": "ping"}]}


def run(label, fn):
    ms, status, durs, hits = [], [], [], []
    for _ in range(N):
        before = lab.mock_stats(lab.PRIMARY)["count"]
        t0 = time.perf_counter()
        r = fn()
        ms.append((time.perf_counter() - t0) * 1000)
        status.append(r.status_code)
        d = r.headers.get("x-litellm-response-duration-ms")
        if d:
            durs.append(float(d))
        hits.append(lab.mock_stats(lab.PRIMARY)["count"] - before)
        time.sleep(0.2)
    med = statistics.median(ms)
    print(f"  {label:<28} 中央値={med:>7.0f}ms  最大={max(ms):>7.0f}ms  status={sorted(set(status))}"
          f"  primary到達/回={statistics.median(hits):.0f}"
          + (f"  litellm自己計測={statistics.median(durs):.0f}ms" if durs else "")
          + f"\n      各回(ms)={[round(x) for x in ms]}")
    return med


def via_proxy(model):
    return lambda: lab.chat(key, model, timeout=60)


print(f"プライマリ=500エラー状態で各{N}回")
a = run("A. モック直接", lambda: httpx.post(f"{lab.PRIMARY}/v1/chat/completions", json={"model": "x", **BODY}, timeout=30))
b = run("B. LiteLLM経由 bench-llm", via_proxy("bench-llm"))
c = run("C. LiteLLM経由 main-llm(FB)", via_proxy("main-llm"))
d = run("D. LiteLLM経由 backup-llm", via_proxy("backup-llm"))

print("\n判定:")
if b > 1000 and a < 300:
    print("  → B(フォールバックなし)が遅い: エラー処理そのものに約%.1f秒かかっている。" % (b / 1000))
    print("    疑う点: 失敗ログのDB書き込み / SDKの再試行(primary到達/回が2以上なら該当) / LiteLLMのバージョン差")
elif c > 1000 and b <= 1000:
    print("  → Bは速いがCが遅い: フォールバック経路で待ちが入っている。")
elif max(b, c) <= 1000:
    print("  → どの経路も1秒未満: この環境では再現しない。")
else:
    print("  → 切り分け不能。A(モック直接)も遅い場合は、モック/Docker/ネットワーク側を疑う。")
lab.reset_mocks()
