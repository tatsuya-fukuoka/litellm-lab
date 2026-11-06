"""フォールバックとクールダウンの挙動を確認する。

シナリオ
  1. 平常時: main-llm はプライマリが応答する。
  2. プライマリ500エラーが続く: 長めの連続リクエスト(既定40件・約15秒)で、すべて成功するか。
     リクエストごとに「プライマリに到達した回数」「リトライ/フォールバックのヘッダ」を記録する。
     ※ バックアップまでクールダウンに入って 429 になる事象(README参照)は、
        クールダウン有効の設定で数秒〜数十秒続けたときに初めて出る。短い試験では見逃す。
  3. プライマリ遅延(タイムアウト超過): タイムアウト後にバックアップへ回る。
  4. 復旧: プライマリを戻し、プライマリへ戻る。

設定に合わせて環境変数を指定する（既定 = litellm_config.yaml）
  COOLDOWNS=off|on   NUM_RETRIES=0   REQUEST_TIMEOUT=5
  クールダウン有効時のみ: ALLOWED_FAILS=2  COOLDOWN_TIME=10
  N_BURST=40  BURST_PACE=0.3(秒)
問題再現: configs/cooldown_pitfall.yaml を使い COOLDOWNS=on NUM_RETRIES=1 で実行する。
注意: モックは1ワーカー(MOCK_WORKERS=1)で動かすこと。複数だと障害注入が一部にしか効かない。
"""
import os
import statistics
import time

import lab

COOLDOWNS_ON = os.environ.get("COOLDOWNS", "off").lower() == "on"
NUM_RETRIES = int(os.environ.get("NUM_RETRIES", "0"))
ALLOWED_FAILS = int(os.environ.get("ALLOWED_FAILS", "2"))
COOLDOWN = float(os.environ.get("COOLDOWN_TIME", "10"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "5"))
N_BURST = int(os.environ.get("N_BURST", "40"))
PACE = float(os.environ.get("BURST_PACE", "0.3"))

lab.wait_ready()
lab.reset_mocks()
c = lab.Checks()
key = lab.lab_key(models=["main-llm", "backup-llm"])
print(f"設定: cooldowns={'on' if COOLDOWNS_ON else 'off'} num_retries={NUM_RETRIES} request_timeout={REQUEST_TIMEOUT:.0f}s"
      + (f" allowed_fails={ALLOWED_FAILS} cooldown_time={COOLDOWN:.0f}s" if COOLDOWNS_ON else ""))


def who(r):
    try:
        return r.json()["choices"][0]["message"]["content"].replace("reply from ", "")
    except Exception:
        return f"HTTP{r.status_code}"


def hdr(r, name):
    return r.headers.get(name, "-")


print("1. 平常時")
r = lab.chat(key, "main-llm")
c.check("プライマリが応答", who(r) == "mock-primary", who(r))

print(f"2. プライマリが500を返し続ける（{N_BURST}件・逐次・間隔{PACE}s）")
lab.mock(lab.PRIMARY, "/admin/mode", mode="error", status=500)
rows = []  # (idx, who, ms, primary_delta, retries, fallbacks, t_start, t_end, cum_hits, status)
cum = 0
for i in range(1, N_BURST + 1):
    before = lab.mock_stats(lab.PRIMARY)["count"]
    t0 = time.monotonic()
    r = lab.chat(key, "main-llm")
    t1 = time.monotonic()
    delta = lab.mock_stats(lab.PRIMARY)["count"] - before
    cum += delta
    rows.append((i, who(r), (t1 - t0) * 1000, delta, hdr(r, "x-litellm-attempted-retries"),
                 hdr(r, "x-litellm-attempted-fallbacks"), t0, t1, cum, r.status_code))
    time.sleep(PACE)

# 表示は先頭8件・失敗が出た件・末尾3件に絞る
bad = [r_[0] for r_ in rows if r_[9] != 200]
show = sorted(set(range(1, min(8, N_BURST) + 1)) | set(bad[:5]) | set(range(max(1, N_BURST - 2), N_BURST + 1)))
print("  #   応答元        ms   primary到達  retries  fallbacks  累計到達")
for i in show:
    _, w, ms, d, rt, fb, _, _, cm, _ = rows[i - 1]
    print(f"  {i:>3} {w:<12} {ms:>6.0f}   {d:>5}      {rt:>5}    {fb:>5}     {cm:>5}")
if len(show) < N_BURST:
    print(f"  ...（全{N_BURST}件中{len(show)}件を表示）")

ok_n = sum(1 for r_ in rows if r_[9] == 200 and r_[1] == "mock-backup")
c.check(f"全{N_BURST}件が成功(バックアップが応答)", ok_n == N_BURST,
        f"成功={ok_n}/{N_BURST}" + (f" / 最初の失敗={bad[0]}件目" if bad else ""))
n429 = sum(1 for r_ in rows if r_[9] == 429)
c.check("429(all_deployments_in_cooldown)が出ない", n429 == 0,
        f"429={n429}件" + (" → バックアップまでクールダウンに入った可能性。README参照" if n429 else ""))
med = statistics.median(r_[2] for r_ in rows if r_[9] == 200) if ok_n else float("nan")
c.note(f"成功リクエストの所要時間 中央値={med:.0f}ms / 最大={max(r_[2] for r_ in rows):.0f}ms / 合計{rows[-1][7] - rows[0][6]:.1f}s")
if NUM_RETRIES >= 1 and med > 1000:
    c.note("中央値が1秒超: 死んだプライマリへのリトライ待ちが毎回入っている(num_retries>=1)")

if COOLDOWNS_ON:
    # クールダウンの開始 = プライマリが初めてスキップされた(到達0になった)リクエストの直前の試行。
    # そこから COOLDOWN 秒(余裕1秒)の間に始まったリクエストが、プライマリに触れていないことを確認する。
    first_skip = next((k for k, r_ in enumerate(rows) if r_[3] == 0 and rows[k - 1][8] >= ALLOWED_FAILS and k > 0), None)
    if first_skip is None:
        c.note(f"プライマリが一度もスキップされなかった(累計到達={cum})。クールダウンに入っていない")
    else:
        t_cool = rows[first_skip - 1][7]
        window = [r_ for r_ in rows[first_skip:] if r_[6] < t_cool + COOLDOWN - 1.0]
        viol = [r_[0] for r_ in window if r_[3] > 0]
        c.check("クールダウン中はプライマリに触れない", not viol,
                f"{first_skip + 1}件目からスキップ開始 / 窓内{len(window)}件 / 違反={viol}" if viol
                else f"{first_skip + 1}件目からスキップ開始 / 窓内{len(window)}件すべて到達0")
else:
    c.check("クールダウン無効なら毎回プライマリに到達する(想定どおり)", cum >= N_BURST, f"到達={cum}/{N_BURST}件")

print("3. プライマリが遅い(タイムアウト超過)")
lab.mock(lab.PRIMARY, "/admin/mode", mode="slow", delay_ms=int(REQUEST_TIMEOUT * 1000 + 3000))
if COOLDOWNS_ON:
    time.sleep(COOLDOWN + 2)  # クールダウンを明けさせ、プライマリへ再び振られる状態にする
t0 = time.perf_counter()
r = lab.chat(key, "main-llm", timeout=120)
ms = (time.perf_counter() - t0) * 1000
print(f"  応答元={who(r)} 所要={ms:.0f}ms retries={hdr(r, 'x-litellm-attempted-retries')} fallbacks={hdr(r, 'x-litellm-attempted-fallbacks')}")
c.check("タイムアウト後にバックアップが応答", who(r) == "mock-backup", who(r))
c.check(f"所要時間 >= request_timeout({REQUEST_TIMEOUT:.0f}s)", ms >= REQUEST_TIMEOUT * 1000 * 0.9, f"{ms:.0f}ms")
c.note(f"目安: timeout × (1+retries) = {REQUEST_TIMEOUT * (1 + NUM_RETRIES):.0f}s + 待ち。観測={ms / 1000:.1f}s")

print("4. 復旧")
lab.mock(lab.PRIMARY, "/admin/mode", mode="ok")
if COOLDOWNS_ON:
    time.sleep(COOLDOWN + 2)  # クールダウン経過待ち
seen = {who(lab.chat(key, "main-llm")) for _ in range(5)}
print("  応答元:", seen)
c.check("プライマリに戻る", "mock-primary" in seen, str(seen))

lab.reset_mocks()
c.finish()
