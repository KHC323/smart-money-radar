#!/usr/bin/env python3
import asyncio
import httpx

SYMBOLS = [
    'BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT',
    'ADAUSDT','AVAXUSDT','DOGEUSDT','DOTUSDT','LINKUSDT',
    'LTCUSDT','ATOMUSDT','NEARUSDT','ARBUSDT','OPUSDT',
]
INITIAL   = 100.0
MAX_POS   = 5
POS_PCT   = 0.20   # 每筆 20%
SL        = -0.05  # 止損 -5%
TP        =  0.15  # 止盈 +15%
MIN_HOLD  = 3      # 最短持倉天數

async def get_klines(sym, interval='1d', limit=260):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval={interval}&limit={limit}"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, headers={"User-Agent": "Backtest/1.0"})
        return r.json() if r.status_code == 200 else []

def calc_cvd(klines):
    cvd, pts = 0, []
    for k in klines:
        buy = float(k[9]); total = float(k[5])
        cvd += buy - (total - buy)
        pts.append(cvd)
    return pts

def signal_1d(klines):
    """日線訊號"""
    if len(klines) < 15:
        return 'neutral'
    cvd   = calc_cvd(klines)
    close = [float(k[4]) for k in klines]
    cvd_up    = (sum(cvd[-5:])/5) > (sum(cvd[-10:-5])/5)
    price_up  = close[-1] > (sum(close[-10:])/10)
    candle_up = close[-1] > float(klines[-1][1])
    if cvd_up and price_up and candle_up:   return 'long'
    if not cvd_up and not price_up:         return 'short'
    return 'neutral'

def signal_4h(klines_4h):
    """4H 訊號：CVD 趨勢 + 價格在均線上"""
    if len(klines_4h) < 20:
        return 'neutral'
    cvd   = calc_cvd(klines_4h)
    close = [float(k[4]) for k in klines_4h]
    # 用最近8根4H K棒（約2天）vs 前8根
    cvd_up   = (sum(cvd[-8:])/8) > (sum(cvd[-16:-8])/8)
    price_up = close[-1] > (sum(close[-12:])/12)
    if cvd_up and price_up:   return 'long'
    if not cvd_up and not price_up: return 'short'
    return 'neutral'

async def run():
    print("\n" + "═"*62)
    print("  主力雷達 v3 │ 30 天模擬交易回測（改進版）")
    print(f"  起始資金 ${INITIAL:.2f} │ 止損 {SL*100:.0f}% │ 止盈 {TP*100:.0f}%")
    print(f"  最短持倉 {MIN_HOLD} 天 │ 進場需日線+4H雙重確認")
    print(f"  選股池: {' '.join(s[:-4] for s in SYMBOLS)}")
    print("═"*62)

    print("\n📡 載入歷史數據中...\n")
    data_1d = {}
    data_4h = {}
    for sym in SYMBOLS:
        k1d = await get_klines(sym, '1d', 80)
        if len(k1d) >= 32:
            data_1d[sym] = k1d
        await asyncio.sleep(0.1)
        k4h = await get_klines(sym, '4h', 260)
        if len(k4h) >= 20:
            data_4h[sym] = k4h
        await asyncio.sleep(0.1)

    loaded = [s for s in data_1d if s in data_4h]
    print(f"   ✓ 載入 {len(loaded)} 個幣種（日線 + 4H）\n")

    balance      = INITIAL
    positions    = {}   # sym -> {entry, qty, cost, day_in}
    trades       = []
    equity_curve = []

    for day in range(30):
        idx_1d = day - 30   # negative slice: simulate "today" = historical day

        # 4H offset: each day = 6 bars of 4H
        idx_4h = (day - 30) * 6

        # ── Build signal map ──
        sigs = {}
        for sym in loaded:
            klines_1d = data_1d[sym]
            klines_4h = data_4h[sym]

            avail_1d = klines_1d[:idx_1d] if idx_1d < 0 else klines_1d
            avail_4h = klines_4h[:idx_4h] if idx_4h < 0 else klines_4h

            if len(avail_1d) < 15 or len(avail_4h) < 20:
                continue

            s1d = signal_1d(avail_1d)
            s4h = signal_4h(avail_4h)

            # 雙重確認：日線 long + 4H long → 進場
            # 出場用：日線 short/neutral
            price = float(avail_1d[-1][4])
            sigs[sym] = {'sig_1d': s1d, 'sig_4h': s4h, 'price': price}

        # ── EXIT ──
        for sym in list(positions.keys()):
            if sym not in sigs:
                continue
            px  = sigs[sym]['price']
            pos = positions[sym]
            ret = (px - pos['entry']) / pos['entry']
            held = day + 1 - pos['day_in']
            reason = None

            if ret <= SL:
                reason = f"止損 {ret*100:+.1f}%"
            elif ret >= TP:
                reason = f"止盈 {ret*100:+.1f}%"
            # 只有持倉滿 MIN_HOLD 天才允許「訊號轉弱」出場
            elif held >= MIN_HOLD and sigs[sym]['sig_1d'] in ('short', 'neutral') and ret < 0:
                reason = f"訊號轉弱({held}天) {ret*100:+.1f}%"

            if reason:
                proceeds = pos['qty'] * px
                pnl = proceeds - pos['cost']
                balance += proceeds
                trades.append(dict(day=day+1, type='EXIT', sym=sym[:-4],
                                   entry=pos['entry'], exit_px=px,
                                   pnl=pnl, pnl_pct=ret*100, reason=reason))
                del positions[sym]

        # ── ENTRY ──
        slots = MAX_POS - len(positions)
        if slots > 0:
            # 雙重確認：日線 long AND 4H long
            candidates = [
                (sym, v) for sym, v in sigs.items()
                if v['sig_1d'] == 'long' and v['sig_4h'] == 'long'
                and sym not in positions
            ]
            for sym, v in candidates[:slots]:
                alloc = balance * POS_PCT
                if alloc < 1.5:
                    continue
                qty = alloc / v['price']
                balance -= alloc
                positions[sym] = dict(entry=v['price'], qty=qty,
                                      cost=alloc, day_in=day+1)
                trades.append(dict(day=day+1, type='ENTRY', sym=sym[:-4],
                                   price=v['price'], size=alloc,
                                   confirm='1D+4H'))

        # Equity snapshot
        pos_val = sum(p['qty'] * sigs[sym]['price']
                      for sym, p in positions.items() if sym in sigs)
        equity_curve.append(balance + pos_val)

    # ── Close all remaining ──
    for sym, pos in list(positions.items()):
        px  = float(data_1d[sym][-1][4])
        ret = (px - pos['entry']) / pos['entry']
        proceeds = pos['qty'] * px
        pnl = proceeds - pos['cost']
        balance += proceeds
        trades.append(dict(day=30, type='EXIT', sym=sym[:-4],
                           entry=pos['entry'], exit_px=px,
                           pnl=pnl, pnl_pct=ret*100, reason='月末結算'))

    # ── Print trades ──
    print(f"{'─'*62}")
    print("  交易紀錄")
    print(f"{'─'*62}")
    for t in trades:
        if t['type'] == 'ENTRY':
            print(f"  Day {t['day']:02d}  📥 買入  {t['sym']:<7}  @${t['price']:.4f}  投入 ${t['size']:.2f}  [{t['confirm']}]")
        else:
            icon = '✅' if t['pnl'] >= 0 else '❌'
            print(f"  Day {t['day']:02d}  {icon} 賣出  {t['sym']:<7}  {t['pnl_pct']:+.2f}%  P&L ${t['pnl']:+.3f}  [{t['reason']}]")

    # ── Stats ──
    closed  = [t for t in trades if t['type']=='EXIT']
    wins    = [t for t in closed if t['pnl'] > 0]
    losses  = [t for t in closed if t['pnl'] <= 0]
    entries = [t for t in trades if t['type']=='ENTRY']
    total_ret = (balance - INITIAL) / INITIAL * 100
    max_eq = max(equity_curve) if equity_curve else INITIAL
    min_eq = min(equity_curve) if equity_curve else INITIAL
    max_dd = (max_eq - min_eq) / max_eq * 100

    print(f"\n{'═'*62}")
    print("  📊 回測結果")
    print(f"{'═'*62}")
    print(f"  起始資金    ${INITIAL:.2f}")
    print(f"  最終資金    ${balance:.2f}")
    print(f"  總報酬      {total_ret:+.2f}%")
    print(f"  最大回撤    -{max_dd:.2f}%")
    print(f"  交易筆數    {len(entries)} 筆進場")
    if closed:
        wr = len(wins)/len(closed)*100
        avg_win  = sum(t['pnl'] for t in wins)/len(wins)   if wins   else 0
        avg_loss = sum(t['pnl'] for t in losses)/len(losses) if losses else 0
        print(f"  勝率        {wr:.0f}%  ({len(wins)}勝 / {len(losses)}敗)")
        print(f"  平均獲利    ${avg_win:+.3f}")
        print(f"  平均虧損    ${avg_loss:+.3f}")
        if avg_loss < 0:
            print(f"  獲利因子    {abs(avg_win/avg_loss):.2f}x")
    print(f"{'═'*62}")
    print("\n⚠️  歷史回測不代表未來績效。未計入手續費（約 0.04%/筆）。")
    print()

asyncio.run(run())
