"""予算・許可モデル・レート制限が、仮想キー単位で効くかを確認する。

シナリオ
  A. max_budget=0.010 USD のキー。1リクエスト=0.004 USD なので、3回目までは通り、
     スペンドが上限を超えた時点で以降は拒否されるはず。
  B. models=["main-llm"] のキーで backup-llm を呼ぶと拒否されるはず。
  C. rpm_limit=5 のキーで 12 回連打すると、一部が 429 になるはず。
"""
import time

import httpx

import lab

if lab.NO_DB:
    raise SystemExit("SKIP: 仮想キー・予算の検証にはPostgreSQL(DB)が必要です (LAB_NO_DB=1 では実行不可)")
lab.wait_ready()
lab.reset_mocks()
c = lab.Checks()

print("A. 予算上限 (max_budget=0.010 / 1リクエスト=%.3f USD)" % lab.COST_PER_REQUEST)
key = lab.gen_key(models=["main-llm"], max_budget=0.010)
statuses = []
with httpx.Client() as cli:
    for i in range(1, 11):
        r = lab.chat(key, "main-llm", cli)
        statuses.append(r.status_code)
        time.sleep(1.2)  # 費用の書き込み(proxy_batch_write_at)を待つ
        if r.status_code != 200:
            break
print("  status推移:", statuses)
first_block = next((i + 1 for i, s in enumerate(statuses) if s != 200), None)
c.check("最初の3回は通る", statuses[:3] == [200, 200, 200], str(statuses[:3]))
c.check("上限超過後に拒否される", first_block is not None, f"最初に拒否された回: {first_block}")
if first_block:
    c.check("拒否は想定範囲(4〜5回目)", 4 <= first_block <= 5, f"{first_block}回目")
# 費用のDB反映は proxy_batch_write_at(5秒) ごとのバッチ書き込み。上限の判定(4回目の拒否)は即時だが、
# /key/info が返すspendは遅れて追いつく。直後に読むと途中の値になるので、一致するまで最大30秒ポーリングする。
exp = lab.COST_PER_REQUEST * statuses.count(200)
t_wait = time.time()
spend, history = 0.0, []
while time.time() - t_wait < 30:
    spend = lab.key_info(key).get("spend", 0)
    history.append(round(spend, 4))
    if abs(spend - exp) < 1e-6:
        break
    time.sleep(1)
lag = time.time() - t_wait
c.check("記録された費用が 成功回数×単価 に一致", abs(spend - exp) < 1e-6,
        f"spend={spend:.4f} expected={exp:.4f} / 一致までの待ち={lag:.1f}s")
if len(set(history)) > 1:
    c.note(f"spendの推移(1秒ごと)={history}  → DB反映はバッチ書き込みで遅れる(上限の判定自体は即時)")

print("B. 許可モデル")
key_b = lab.gen_key(models=["main-llm"])
r = lab.chat(key_b, "backup-llm")
c.check("許可外モデルは拒否", r.status_code in (400, 401, 403), f"status={r.status_code}")
r = lab.chat(key_b, "main-llm")
c.check("許可モデルは通る", r.status_code == 200, f"status={r.status_code}")

print("C. レート制限 (rpm_limit=5)")
key_c = lab.gen_key(models=["main-llm"], rpm_limit=5)
with httpx.Client() as cli:
    codes = [lab.chat(key_c, "main-llm", cli).status_code for _ in range(12)]
print("  status:", codes)
c.check("5回までは通る", codes[:5] == [200] * 5, str(codes[:5]))
c.check("超過分に429が出る", 429 in codes, f"429の件数={codes.count(429)}")

c.finish()
