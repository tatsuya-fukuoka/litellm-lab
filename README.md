# litellm-lab

LiteLLM Proxy の**予算制限・フォールバック・オーバーヘッド**を、実際のLLMキーなしで実測する検証キットです。
上流は自作のモックLLM（OpenAI互換）で、障害や遅延を実行時に注入できます。

- 解説記事: Zenn「LiteLLM Proxyを実測した」（公開後にURLを追記）
- 実測データ: [RESULTS.md](RESULTS.md)

```
client ─▶ LiteLLM Proxy ─┬─▶ mock-primary  (main-llm / bench-llm)
            │            └─▶ mock-backup   (backup-llm)
            └─▶ PostgreSQL（仮想キー・費用）
```

## 必要なもの
- Docker / Docker Compose
- [uv](https://docs.astral.sh/uv/)（または Python 3.10+ と `pip install httpx`）

## クイックスタート（1台で動かす）
```bash
git clone <このリポジトリ> && cd litellm-lab
./scripts/init_env.sh                  # .env を作成（鍵とパスワードをランダム生成）
docker compose up -d --build           # 初回はLiteLLMの起動に1分ほどかかる

uv run python scripts/test_budget.py       # 予算・許可モデル・レート制限
uv run python scripts/test_fallback.py     # フォールバック
uv run python scripts/bench_overhead.py    # オーバーヘッド（結果は results/ にCSV/JSON）
```
`init_env.sh` を使わない場合は、`cp .env.example .env` のあと `REPLACE_ME` を必ず置き換えてください（未設定のままだとcomposeとスクリプトが止まります）。
Windows（PowerShell）では `.env.example` を手でコピーし、鍵を自分で設定してください。

> **注意:** ローカルのDocker Desktopでは、ポート転送などの影響で数値がぶれます。性能の計測は、専用のLinux環境（次節）で行うことを推奨します。

## 別マシンから測る（サーバー1台＋クライアント1台）
同じVPC・同じAZのインスタンスを2台使います。

1. **サーバー側**: `.env` に `BIND_ADDR=0.0.0.0` と `LITELLM_WORKERS` を設定し、`docker compose up -d --build`。
   セキュリティグループは、**クライアントのSGからのTCP 4000 / 19001 / 19002 のみ**許可します。`0.0.0.0/0` にしないこと（マスターキーと、認証のないモック管理APIが露出します）。
2. **クライアント側**: リポジトリを置き、`.env` にサーバーのプライベートIPを書きます（`.env.example` 末尾参照）。`LITELLM_MASTER_KEY` はサーバーと同じ値にします。
3. **workerを変えて測る**: サーバー側で `LITELLM_WORKERS` を変えて作り直し、クライアントで計測します。
   ```bash
   # サーバー側
   sed -i 's|^LITELLM_WORKERS=.*|LITELLM_WORKERS=2|' .env
   docker compose up -d --force-recreate litellm
   docker compose config | grep -A1 num_workers      # 値が反映されたか確認
   # クライアント側（LiteLLMの起動を待ってから）
   uv run python scripts/bench_overhead.py --conc 1 10 50 --n 5000
   ```
4. `test_fallback.py` は `MOCK_WORKERS=1` のまま実行します（障害注入の状態がプロセスごとのため）。
5. 計測中は、サーバー側で `docker stats` や `top` でCPUを確認してください。モニタは計測に影響するので、記録する数値は別の実行から取ります。

## 設定（`.env`）
| 変数 | 既定 | 意味 |
| --- | --- | --- |
| `LITELLM_IMAGE` | `…litellm:v1.108.0` | 固定タグにする（`latest` 禁止）。タグの実在は公式リリースで確認 |
| `LITELLM_WORKERS` | 1 | LiteLLMのworker数 |
| `MOCK_WORKERS` | 1 | モックのworker数。並列を上げて「直接」側が先に詰まる時だけ増やす |
| `LITELLM_CONFIG` | `./litellm_config.yaml` | 使う設定。クールダウンの再現は `./configs/cooldown_pitfall.yaml` |
| `BIND_ADDR` | `127.0.0.1` | 公開アドレス。別マシンから測る時だけ `0.0.0.0` |
| `LITELLM_PORT` / `MOCK_*_PORT` | 4000 / 19001 / 19002 | ホスト側ポート（衝突したら変更）|

## ファイル
| ファイル | 役割 |
| --- | --- |
| `docker-compose.yml` | db / mock-primary / mock-backup / litellm |
| `litellm_config.yaml` | 既定の設定（`num_retries: 0` + `disable_cooldowns: true`）|
| `configs/cooldown_pitfall.yaml` | クールダウン有効＋リトライ1回。バックアップまで429になる現象を再現 |
| `litellm_config.nodb.yaml` | DBなしで動かす設定（`LAB_NO_DB=1`。予算テストは実行不可）|
| `mock_llm/` | モックLLM。`/admin/mode` で `error` / `slow` を注入 |
| `scripts/test_budget.py` | 仮想キーの予算・許可モデル・RPM制限 |
| `scripts/test_fallback.py` | 500・タイムアウト時のフォールバックと復旧 |
| `scripts/bench_overhead.py` | 直接 vs 経由のp50/p95/p99差と `x-litellm-overhead-duration-ms`。直接側の飽和を警告 |
| `scripts/diagnose_error_latency.py` | 障害時の遅延を4経路で切り分け |
| `scripts/sweep_workers.py` | 1台構成で `LITELLM_WORKERS` を変えながらベンチを回す |
| `scripts/init_env.sh` | `.env` をランダム鍵つきで作成 |

### 仕様メモ
- 課金額は「モックの usage（100/100トークン）× 設定の単価」で、1リクエスト = 0.004 USD。
- `proxy_batch_write_at: 5` は検証を速くする短縮値です（既定は60秒）。費用の反映は最大でこの間隔ぶん遅れます。
- `bench-llm` はフォールバック設定の影響を受けない別名で、オーバーヘッド計測はこちらを使います。
- 並列度が高いと「直接」側（モックまたはクライアント）が先に頭打ちになり、差が意味を失います。`baseline_saturated` の警告が出た行は使わないでください。

## 結果の要約
詳細は [RESULTS.md](RESULTS.md) を参照してください。
- 予算は、上限を**超えた後の次のリクエストから**拒否される（上限0.010に対し0.012まで進んだ）
- 低並列のオーバーヘッドは、プロキシ1段で約9ms
- スループットはworker数にほぼ比例（1/2/4 workersで 101/188/311 RPS）
- クールダウン有効＋リトライ1回だと、バックアップまで429になる構成があった（原因は未特定）

## セキュリティ
- 検証専用です。モックの管理APIは無認証なので、公開ネットワークに出さないでください。
- 鍵・パスワードは `.env` に置き、コミットしないでください（`.gitignore` 済み）。
- イメージ・ライブラリは固定して使います（2026年3月にLiteLLMのPyPIパッケージが改ざんされた事例があります）。

## ライセンス
MIT（[LICENSE](LICENSE)）。
