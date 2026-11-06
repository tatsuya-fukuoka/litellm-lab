"""LiteLLMが足すオーバーヘッドを実測する。

方法
  同じ並列度・同じ件数で、(a)モックLLMへ直接 と (b)LiteLLM経由 を叩き、
  レイテンシの p50/p95/p99 の差を「オーバーヘッド」とする。
  あわせて、LiteLLM自身が返す x-litellm-overhead-duration-ms ヘッダも集計する。

読み方の注意
  * 「直接」側のRPSが、並列を上げても伸びない(または落ちる)場合は、モック・クライアント・
    Docker のポート転送のどれかが先に飽和している。その並列度の「差」は信頼できないので、
    baseline_saturated=True として警告を出す。MOCK_WORKERS を増やすか、並列度を下げること。
  * クライアントとプロキシが同じマシンにいると、CPUを奪い合って悪化する。
"""
import argparse
import asyncio
import csv
import json
import statistics
import time
from pathlib import Path

import httpx

import lab


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p / 100))] if xs else float("nan")


async def run(url, headers, body, n, conc, collect_overhead):
    lat, over, errors = [], [], 0
    sem = asyncio.Semaphore(conc)
    limits = httpx.Limits(max_connections=conc * 2, max_keepalive_connections=conc * 2)
    async with httpx.AsyncClient(limits=limits, timeout=60) as cli:
        async def one():
            nonlocal errors
            async with sem:
                t0 = time.perf_counter()
                try:
                    r = await cli.post(url, headers=headers, json=body)
                except httpx.HTTPError:
                    errors += 1
                    return
                dt = (time.perf_counter() - t0) * 1000
                if r.status_code != 200:
                    errors += 1
                    return
                lat.append(dt)
                if collect_overhead:
                    h = r.headers.get("x-litellm-overhead-duration-ms")
                    if h is not None:
                        over.append(float(h))
        t0 = time.perf_counter()
        await asyncio.gather(*[one() for _ in range(n)])
        wall = time.perf_counter() - t0
    return lat, over, errors, len(lat) / wall


def measure(n=2000, conc_list=(1, 10, 50), warmup=200, verbose=True):
    """全並列度で測定して行(dict)のリストを返す。sweep_workers.py からも使う。"""
    lab.wait_ready()
    lab.reset_mocks()
    key = lab.lab_key(models=["bench-llm"])  # 予算・レート無制限の仮想キー
    body_direct = {"model": "mock-primary", "messages": [{"role": "user", "content": "ping"}]}
    body_proxy = {"model": "bench-llm", "messages": [{"role": "user", "content": "ping"}]}
    direct_url = f"{lab.PRIMARY}/v1/chat/completions"
    proxy_url = f"{lab.PROXY}/v1/chat/completions"
    proxy_headers = {"Authorization": f"Bearer {key}"}

    rows, best_direct_rps = [], 0.0
    for conc in conc_list:
        asyncio.run(run(proxy_url, proxy_headers, body_proxy, warmup, conc, False))
        d_lat, _, d_err, d_rps = asyncio.run(run(direct_url, {}, body_direct, n, conc, False))
        p_lat, p_over, p_err, p_rps = asyncio.run(run(proxy_url, proxy_headers, body_proxy, n, conc, True))
        # 直接側のRPSが、これまでの最大の7割を割ったら、ベースラインが飽和したとみなす
        saturated = best_direct_rps > 0 and d_rps < best_direct_rps * 0.7
        best_direct_rps = max(best_direct_rps, d_rps)
        row = {"conc": conc, "n": n, "errors_direct": d_err, "errors_proxy": p_err,
               "rps_direct": round(d_rps, 1), "rps_proxy": round(p_rps, 1),
               "baseline_saturated": saturated}
        for p in (50, 95, 99):
            row[f"direct_p{p}"] = round(pct(d_lat, p), 2)
            row[f"proxy_p{p}"] = round(pct(p_lat, p), 2)
            row[f"diff_p{p}"] = round(pct(p_lat, p) - pct(d_lat, p), 2)
            row[f"hdr_p{p}"] = round(pct(p_over, p), 2) if p_over else None
        row["hdr_mean"] = round(statistics.fmean(p_over), 2) if p_over else None
        rows.append(row)
        if verbose:
            print(f"conc={conc:>3} | 直接 p50/p95/p99 = {row['direct_p50']}/{row['direct_p95']}/{row['direct_p99']} ms"
                  f" | 経由 = {row['proxy_p50']}/{row['proxy_p95']}/{row['proxy_p99']} ms"
                  f" | 差 = {row['diff_p50']}/{row['diff_p95']}/{row['diff_p99']} ms"
                  f" | hdr = {row['hdr_p50']}/{row['hdr_p95']}/{row['hdr_p99']} ms"
                  f" | RPS {row['rps_direct']}→{row['rps_proxy']} | err {d_err}/{p_err}")
            if saturated:
                print(f"  ⚠ conc={conc}: 直接側が飽和({d_rps:.0f} RPS < 最大{best_direct_rps:.0f}の7割)。"
                      "この並列度の『差』は信頼できません。MOCK_WORKERSを増やすか並列度を下げてください。")
    return rows


def save(rows, name="overhead"):
    out = Path(__file__).resolve().parent.parent / "results"
    out.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out / f"{name}-{stamp}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    with open(out / f"{name}-{stamp}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"saved: results/{name}-{stamp}.(json|csv)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000, help="1条件あたりのリクエスト数")
    ap.add_argument("--conc", type=int, nargs="+", default=[1, 10, 50])
    ap.add_argument("--warmup", type=int, default=200)
    args = ap.parse_args()
    save(measure(args.n, args.conc, args.warmup))


if __name__ == "__main__":
    main()
