# 下單程式

每位用戶用自己的電腦和永豐帳戶。GitHub 上共用的是每週權重。金額、API、憑證、帳本留在自己的電腦。

## 安裝

```bash
git clone https://github.com/YirenNTU/order-desk.git
cd order-desk
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

執行 `python -m order_desk` 時會自動讀這個 `.env`，不用另外 export。

`config.json` 照 `config.example.json`。

- `name` 必須等於 `signal.json` 的 `strategy_id`
- `extra_twd`：這次要加進這個策略的現金。第一次買填這裡。它會留到你改回 `0`；沒改回的話，下次計畫會再加一次
- `cash_out_twd`：要提出的金額。大於 0 會賣掉這個策略多出來的股票
- `mode`：`simulation` 模擬，`production` 正式
- `lot_mode`：`odd` 零股，`common` 整張
- `cash_check`：`true` 才檢查證券戶餘額。漲停凍結超過餘額就不送單。預設 `false`，授權交割銀行可以直接送
- `schedule`：台北時間。每週第一個開盤日拉取和下單，週一休市就順延

帳本裡已經有股數的策略都要留在 `config.json`。拿掉之後，整份計畫會拒絕執行。

股數 =（這個策略帳上股票的現值 + `extra_twd` − `cash_out_twd`）× 權重 ÷ 現價。帳本不存總金額，現值是下單當下用股價現算的。

每週權重在 `strategies/<名稱>/signal.json`，有效時間要蓋住當週交易日。

## 下單

```bash
python -m order_desk
python -m order_desk --send --confirm APPROVE
```

第一行是預覽，不送新單，但會先向券商核對委託並寫回帳本。前一個交易日沒成交的單會標成過期。第二行才送出 `config.json` 裡每個策略的單。

買單掛漲停，賣單掛跌停，先賣再買。漲停是出價上限，不保證成交。隔天再執行時，過期的單會放掉，用當天現價重算股數。

## 帳本

成交後的股數記在 `strategies/<名稱>/ledger.json` 的 `positions`，會一直累加。還沒結束的委託暫存在同一個檔案的 `open_orders`。這個檔案不要上傳，`git pull` 也不會改它。

## 每週自動

```bash
python -m order_desk --install-schedule
```

每週第一個開盤日 08:00 做 `git pull --ff-only`，09:20 用這台電腦的 `config.json` 下單。電腦要開著並已登入。當天沒拉成功，或查不到休市日，就不會下單。其他交易日不會自動補單，過了 09:20 也要自己執行上面的下單指令。改了 `schedule` 要再安裝一次。

`git pull` 更新的是 repo 裡已追蹤的檔案，包含 `strategies/<名稱>/signal.json` 的新權重，也包含程式碼。它不會改 `.env`、`config.json`、帳本，也不會動券商那邊的委託。
