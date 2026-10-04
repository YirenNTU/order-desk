# 下單程式

每位用戶用自己的電腦和永豐帳戶。GitHub 上共用的是每週權重。金額、API、憑證、帳本留在自己的電腦。

## 安裝

```bash
cd order_desk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
cp .env.example .env
```

## 設定

`.env` 照 `.env.example` 填自己的 API、帳號、憑證。正式下單加上：

```bash
TRADING_ALLOW_PRODUCTION=YES_I_ACCEPT_REAL_ORDERS
```

`config.json` 照 `config.example.json`。

- `name` 必須等於 `signal.json` 的 `strategy_id`
- `extra_twd`：加進這個策略的現金。第一次買填這裡
- `cash_out_twd`：要提出的金額。大於 0 會賣掉多出來的股票
- `mode`：`simulation` 模擬，`production` 正式
- `lot_mode`：`odd` 零股，`common` 整張
- `schedule`：台北時間。每週第一個開盤日拉取和下單，週一休市就順延

股數 =（這個策略的現值 + `extra_twd` − `cash_out_twd`）× 權重 ÷ 現價。

每週權重在 `strategies/<名稱>/signal.json`，有效時間要蓋住當週交易日。

## 下單

```bash
python -m order_desk
python -m order_desk --send --confirm APPROVE
```

第一行只預覽。買單掛漲停，賣單掛跌停，先賣再買。沒成交的隔天用現價重算再送。

## 每週自動

```bash
python -m order_desk --install-schedule
```

開盤日 08:00 `git pull`，09:20 用這台電腦的設定下單。電腦要開著並已登入。當天沒拉成功，或查不到休市日，就不會下單。改了 `schedule` 要再安裝一次。

成交記在 `strategies/<名稱>/ledger.json`，不要上傳。
