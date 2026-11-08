# 下單程式

每位用戶用自己的電腦和永豐帳戶。GitHub 上共用的是程式和每週權重。金額、API、憑證、帳本、log 留在自己的電腦。

目前發布的策略是 `super_alpha_4`、`super_alpha_5`、`rolling_top_10`。

`git pull` 會更新程式和 `strategies/<名稱>/signal.json`。它不會改 `.env`、`config.json`、`strategies/<名稱>/ledger.json`、`logs/`。

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
- `adjust_positions`：缺欄位或 `true` 會照新權重調倉。設成 `false` 時當週不調這個策略：不跟新權重、不加錢、也不提領，帳上股數原樣留著。策略仍要留在 `config.json`。這個開關會一直維持，直到你改回 `true`
- `mode`：`simulation` 模擬，`production` 正式
- `lot_mode`：`odd` 零股，`common` 整張
- `cash_check`：`true` 才檢查證券戶餘額。漲停凍結超過餘額就不送單。預設 `false`，授權交割銀行可以直接送
- `schedule.enabled`：`false` 時已安裝的排程下一分鐘起不再拉權重、也不下單。缺欄位視為開啟
- `schedule.git_pull` / `schedule.order`：台北時間。從每週第一個開盤日到週五，到了時間就拉權重或補下單；週一休市就順延

帳本裡已經有股數的策略都要留在 `config.json`。拿掉之後，整份計畫會拒絕執行。

股數 =（這個策略帳上股票的現值 + `extra_twd` − `cash_out_twd`）× 權重 ÷ 現價。帳本不存總金額，現值是下單當下用股價現算的。

每週權重在 `strategies/<名稱>/signal.json`，有效時間要蓋住當週交易日。`adjust_positions` 為 `false` 的策略即使 signal 過期，也不會擋住其他策略。

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
python -m order_desk --uninstall-schedule
```

安裝後，LaunchAgent 每分鐘檢查一次。電腦要開著並已登入。

- 每週第一個開盤日到週五：到達 `schedule.git_pull`（預設 08:00）且當天還沒拉成功，就做 `git pull --ff-only`
- 同一區間、到達 `schedule.order`（預設 09:20）、盤中 09:00–13:25、且本週還沒完成下單：當日 pull 成功才送單
- 週一休市會順延到該週下一個交易日
- 第一天沒開機、過了盤中、或送單失敗：後面的交易日會再拉一次並補下單
- 單已送出還沒成交：同一天不再送。下一個交易日先放掉過期單，用帳上實際股數和現價重算，只補還沒到位的部分
- 本週全部成交，或本來就沒有需要的單：後面幾天不再下單
- 週五盤後仍沒完成：留到下週第一個開盤日，用新的一週權重重算
- `logs/last_order.json` 記本週狀態：`completed`、`working`、`failed`。只有 `completed` 算本週做完

要暫停排程：把 `config.json` 的 `schedule.enabled` 設成 `false`，下一分鐘生效。要改時間或星期：改 `schedule.git_pull` / `schedule.order` 即可，不用重裝。只有換電腦、換 Python，或 config 路徑變了，才要再跑 `--install-schedule`。要卸掉 LaunchAgent 用 `--uninstall-schedule`。

當天 pull 失敗，或查不到休市日，就不會下單。也可以隨時手動執行上面的下單指令。

`git pull` 更新的是 repo 裡已追蹤的檔案，包含程式碼和 `strategies/<名稱>/signal.json` 的新權重。它不會改 `.env`、`config.json`、帳本、`logs/`，也不會動券商那邊的委託。
