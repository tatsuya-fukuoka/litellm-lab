"""LiteLLMのワーカー数を変えながらオーバーヘッドを測り、飽和点を探す。

docker compose で litellm を LITELLM_WORKERS=w で作り直しては bench_overhead.measure を回す。
（compose を使わず自分で起動している場合は --no-restart で、手動で変えながら1回ずつ実行する）

例:
  python scripts/sweep_workers.py --workers 1 2 4 --conc 1 10 50 100 --n 1500
注意:
  * ワーカー数はホストのCPU数を超えても伸びない。docker desktop側のCPU割当も確認すること。
  * 結果は results/sweep-*.csv に、ワーカー数ごとの行として保存される。
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import bench_overhead
import lab

ROOT = Path(__file__).resolve().parent.parent


def restart_litellm(workers):
    env = dict(os.environ, LITELLM_WORKERS=str(workers))
    subprocess.run(["docker", "compose", "up", "-d", "--force-recreate", "litellm"],
                   cwd=ROOT, env=env, check=True)
    time.sleep(3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4])
    ap.add_argument("--conc", type=int, nargs="+", default=[1, 10, 50])
    ap.add_argument("--n", type=int, default=1500)
    ap.add_argument("--warmup", type=int, default=200)
    ap.add_argument("--no-restart", action="store_true", help="composeでの再作成をしない")
    args = ap.parse_args()

    all_rows = []
    for w in args.workers:
        print(f"\n=== LITELLM_WORKERS={w} ===")
        if not args.no_restart:
            restart_litellm(w)
        rows = bench_overhead.measure(args.n, args.conc, args.warmup)
        for r in rows:
            all_rows.append({"workers": w, **r})

    print("\n--- まとめ（経由のスループットと p95 差） ---")
    print("workers | conc | proxy RPS | 差p50 | 差p95 | baseline飽和")
    for r in all_rows:
        print(f"{r['workers']:>7} | {r['conc']:>4} | {r['rps_proxy']:>9} | {r['diff_p50']:>5} | {r['diff_p95']:>5} | {r['baseline_saturated']}")
    bench_overhead.save(all_rows, name="sweep")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
