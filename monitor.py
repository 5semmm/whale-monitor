# -*- coding: utf-8 -*-
"""
Hyperliquid 고래 지갑 모니터 → 텔레그램 알림
Hyperdash(hyperdash.com)가 보여주는 데이터의 원본인 Hyperliquid 공개 API를 폴링한다.

감지 항목:
  1. 포지션 변화 (신규 진입 / 청산 / 방향 전환)  - clearinghouseState
  2. 최근 활동 (체결 내역, 폴링 주기 단위로 묶어서 요약) - userFills
  3. 입출금 등 원장 활동                          - userNonFundingLedgerUpdates

텔레그램 토큰이 설정되지 않으면 콘솔 출력만 하는 드라이런 모드로 동작한다.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "whale_state.json"
LOG_PATH = BASE_DIR / "monitor.log"

API_URL = "https://api.hyperliquid.xyz/info"
KST = timezone(timedelta(hours=9))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg):
    line = f"[{datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_telegram_secrets():
    """로컬 실행용 텔레그램 설정. git에 올라가지 않도록 별도 파일로 분리."""
    p = BASE_DIR / "telegram.json"
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def api_post(payload, timeout=15):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        API_URL, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_positions(address):
    """{coin: {szi, entryPx, positionValue, leverage, unrealizedPnl, liqPx}}"""
    data = api_post({"type": "clearinghouseState", "user": address})
    positions = {}
    for ap in data.get("assetPositions", []):
        p = ap.get("position", {})
        coin = p.get("coin")
        szi = float(p.get("szi", 0))
        if not coin or szi == 0:
            continue
        positions[coin] = {
            "szi": szi,
            "entryPx": float(p.get("entryPx") or 0),
            "positionValue": float(p.get("positionValue") or 0),
            "leverage": (p.get("leverage") or {}).get("value"),
            "unrealizedPnl": float(p.get("unrealizedPnl") or 0),
            "liqPx": float(p.get("liquidationPx") or 0),
        }
    account_value = float(data.get("marginSummary", {}).get("accountValue", 0))
    return positions, account_value


def fetch_fills(address):
    return api_post({"type": "userFills", "user": address})


def fetch_ledger(address, start_time_ms):
    return api_post({
        "type": "userNonFundingLedgerUpdates",
        "user": address,
        "startTime": int(start_time_ms),
    })


class Telegram:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(
            token and chat_id
            and "여기에" not in str(token) and "여기에" not in str(chat_id)
        )

    def send(self, text):
        if not self.enabled:
            log("(드라이런 - 텔레그램 미설정) 알림 내용:\n" + text)
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        try:
            req = urllib.request.Request(url, data=payload)
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            log("텔레그램 전송 완료")
        except Exception as e:
            log(f"텔레그램 전송 실패: {e}")


def fmt_usd(v):
    v = abs(v)
    if v >= 1_000_000:
        return f"${v/1_000_000:,.2f}M"
    if v >= 1_000:
        return f"${v/1_000:,.1f}K"
    return f"${v:,.2f}"


def fmt_num(v):
    return f"{v:,.4f}".rstrip("0").rstrip(".")


def side_kr(szi):
    return "롱" if szi > 0 else "숏"


def fmt_time(ms):
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%m/%d %H:%M")


def position_summary(positions, account_value):
    if not positions:
        return "현재 오픈 포지션 없음"
    lines = [f"계좌 가치  {fmt_usd(account_value)}", ""]
    for coin, p in sorted(positions.items()):
        pnl = p["unrealizedPnl"]
        pnl_str = f"{'+' if pnl >= 0 else '-'}{fmt_usd(pnl)}"
        lines.append(f"<b>{coin} {side_kr(p['szi'])}</b>  {fmt_num(abs(p['szi']))}개 @ {fmt_num(p['entryPx'])}")
        lines.append(f"  규모 {fmt_usd(p['positionValue'])} · {p['leverage']}x · 평가손익 {pnl_str}")
    return "\n".join(lines)


def diff_positions(old, new):
    """포지션 신규 진입 / 청산 / 방향 전환 이벤트를 알림 문자열 리스트로 반환"""
    events = []
    for coin, p in new.items():
        if coin not in old:
            liq = f"\n청산가 {fmt_num(p['liqPx'])}" if p['liqPx'] else ""
            events.append(
                f"<b>[신규 진입] {coin} {side_kr(p['szi'])}</b>\n"
                f"{fmt_num(abs(p['szi']))}개 @ {fmt_num(p['entryPx'])}\n"
                f"규모 {fmt_usd(p['positionValue'])}\n"
                f"레버리지 {p['leverage']}x{liq}"
            )
        elif (old[coin]["szi"] > 0) != (p["szi"] > 0):
            events.append(
                f"<b>[방향 전환] {coin}  {side_kr(old[coin]['szi'])} → {side_kr(p['szi'])}</b>\n"
                f"현재 {fmt_num(abs(p['szi']))}개 @ {fmt_num(p['entryPx'])} · {fmt_usd(p['positionValue'])}"
            )
    for coin, p in old.items():
        if coin not in new:
            events.append(
                f"<b>[전량 청산] {coin} {side_kr(p['szi'])}</b>\n"
                f"{fmt_num(abs(p['szi']))}개 포지션 종료"
            )
    return events


DIR_KR = {
    "Open Long": "롱 진입", "Open Short": "숏 진입",
    "Close Long": "롱 청산", "Close Short": "숏 청산",
    "Buy": "매수", "Sell": "매도",
    "Long > Short": "롱→숏 전환", "Short > Long": "숏→롱 전환",
    "Liquidated Isolated Long": "롱 강제청산", "Liquidated Isolated Short": "숏 강제청산",
    "Liquidated Cross Long": "롱 강제청산", "Liquidated Cross Short": "숏 강제청산",
}


def summarize_fills(fills, min_notional):
    """새 체결들을 (코인, 방향) 단위로 묶어 요약. 반환: (요약 문자열 리스트, 최대 time)"""
    groups = {}
    max_time = 0
    for f in fills:
        t = f["time"]
        max_time = max(max_time, t)
        key = (f["coin"], f["dir"])
        g = groups.setdefault(key, {"sz": 0.0, "notional": 0.0, "pnl": 0.0,
                                    "n": 0, "first": t, "last": t})
        sz = float(f["sz"])
        px = float(f["px"])
        g["sz"] += sz
        g["notional"] += sz * px
        g["pnl"] += float(f.get("closedPnl") or 0)
        g["n"] += 1
        g["first"] = min(g["first"], t)
        g["last"] = max(g["last"], t)

    lines = []
    for (coin, direction), g in sorted(groups.items(), key=lambda x: -x[1]["notional"]):
        if g["notional"] < min_notional:
            continue
        avg_px = g["notional"] / g["sz"] if g["sz"] else 0
        when = fmt_time(g["first"])
        if g["last"] != g["first"]:
            when += f" ~ {fmt_time(g['last'])[-5:]}"
        line = (
            f"<b>[체결] {coin} {DIR_KR.get(direction, direction)}</b>\n"
            f"{fmt_num(g['sz'])}개 @ 평균 {fmt_num(avg_px)}\n"
            f"규모 {fmt_usd(g['notional'])} · {g['n']}건 · {when}"
        )
        if abs(g["pnl"]) > 0.01:
            line += f"\n실현손익 {'+' if g['pnl'] >= 0 else '-'}{fmt_usd(g['pnl'])}"
        lines.append(line)
    return lines, max_time


LEDGER_KR = {
    "deposit": "입금", "withdraw": "출금",
    "accountClassTransfer": "계정 간 이체", "internalTransfer": "내부 이체",
    "subAccountTransfer": "서브계정 이체", "spotTransfer": "현물 이체",
    "vaultDeposit": "볼트 입금", "vaultWithdraw": "볼트 출금",
}


def summarize_ledger(updates, last_time):
    lines = []
    max_time = last_time
    for u in updates:
        t = u.get("time", 0)
        if t <= last_time:
            continue
        max_time = max(max_time, t)
        delta = u.get("delta", {})
        kind = delta.get("type", "?")
        usdc = delta.get("usdc") or delta.get("amount") or ""
        amount = f" {fmt_usd(float(usdc))}" if usdc else ""
        lines.append(f"<b>[{LEDGER_KR.get(kind, kind)}]</b>{amount} · {fmt_time(t)}")
    return lines, max_time


def main():
    once = "--once" in sys.argv  # GitHub Actions 등에서 1회 실행 후 종료
    cfg = load_config()
    address = cfg["address"]
    interval = int(cfg.get("poll_interval_sec", 60))
    min_notional = float(cfg.get("min_fill_notional_usd", 0))
    # 우선순위: 환경변수(GitHub Secrets) > telegram.json(로컬) > config.json
    sec = load_telegram_secrets()
    token = (os.environ.get("TELEGRAM_BOT_TOKEN")
             or sec.get("telegram_bot_token") or cfg.get("telegram_bot_token"))
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID")
               or sec.get("telegram_chat_id") or cfg.get("telegram_chat_id"))
    tg = Telegram(token, chat_id)

    short_addr = address[:6] + "..." + address[-4:]
    hyperdash_url = f"https://hyperdash.com/address/{address}"

    log(f"모니터링 시작: {address} ({'1회 실행' if once else f'폴링 {interval}초'})")
    if not tg.enabled:
        log("⚠ 텔레그램 토큰/챗ID 미설정 - 드라이런 모드 (알림은 콘솔에만 출력)")

    state = load_state()
    if state is None:
        positions, account_value = fetch_positions(address)
        fills = fetch_fills(address)
        last_fill_time = max((f["time"] for f in fills), default=0)
        now_ms = int(time.time() * 1000)
        state = {
            "positions": positions,
            "last_fill_time": last_fill_time,
            "last_ledger_time": now_ms,
        }
        save_state(state)
        tg.send(
            f"<b>고래 모니터링 시작</b>  <code>{short_addr}</code>\n"
            f"────────────────\n"
            f"{position_summary(positions, account_value)}\n\n"
            f'<a href="{hyperdash_url}">Hyperdash에서 보기</a>'
        )

    while True:
        try:
            messages = []

            positions, account_value = fetch_positions(address)
            messages += diff_positions(state["positions"], positions)

            fills = fetch_fills(address)
            new_fills = [f for f in fills if f["time"] > state["last_fill_time"]]
            if new_fills:
                fill_lines, max_fill_time = summarize_fills(new_fills, min_notional)
                messages += fill_lines
                state["last_fill_time"] = max(state["last_fill_time"], max_fill_time)

            try:
                ledger = fetch_ledger(address, state["last_ledger_time"] + 1)
                ledger_lines, max_ledger_time = summarize_ledger(
                    ledger, state["last_ledger_time"])
                messages += ledger_lines
                state["last_ledger_time"] = max_ledger_time
            except Exception as e:
                log(f"원장 조회 실패(무시): {e}")

            if messages:
                body = "\n\n".join(messages)
                tg.send(
                    f"<b>고래 활동 감지</b>  <code>{short_addr}</code>\n"
                    f"────────────────\n"
                    f"{body}\n\n"
                    f'<a href="{hyperdash_url}">Hyperdash에서 보기</a>'
                )
                log(f"이벤트 {len(messages)}건 알림")
            else:
                log(f"변화 없음 (포지션 {len(positions)}개, 계좌 {fmt_usd(account_value)})")

            state["positions"] = positions
            save_state(state)

        except KeyboardInterrupt:
            log("사용자 중지")
            break
        except Exception as e:
            if once:
                raise
            log(f"오류 발생(다음 주기에 재시도): {e}")

        if once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
