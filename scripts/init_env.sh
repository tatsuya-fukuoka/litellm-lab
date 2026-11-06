#!/usr/bin/env bash
# .env.example から .env を作り、鍵とパスワードをランダム値にする。既存の .env は上書きしない。
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -e .env ]; then echo ".env は既にあります（上書きしません）"; exit 1; fi
cp .env.example .env
rand() { openssl rand -hex 24; }
sed -i "s|^LITELLM_MASTER_KEY=.*|LITELLM_MASTER_KEY=sk-$(rand)|" .env
sed -i "s|^LITELLM_SALT_KEY=.*|LITELLM_SALT_KEY=sk-$(rand)|" .env
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(rand)|" .env
chmod 600 .env
echo ".env を作成しました。LITELLM_IMAGE のタグ（固定）を確認してください。"
echo "別マシンのクライアントから測る場合は、同じ LITELLM_MASTER_KEY をクライアントの .env にも書いてください。"
