#!/usr/bin/env python3
"""
異常信號回測：量爆增 + OI暴增 策略
偵測高機率暴漲小幣
"""
import asyncio
import httpx

COINS = [
    'FETUSDT','INJUSDT','SUIUSDT','APTUSDT','SEIUSDT',
    'TIAUSDT','STXUSDT','ALGOUSDT','ICPUSDT','NEARUSDT',
    'AAVEUSDT','UNIUSDT','CRVUSDT','LDOUSDT','SNXUSDT',
    'AXSUSDT','SANDUSDT','MANAUSDT','GALAUSDT','ENJUSDT',
    'FILUSDT','GRTUSDT','PENDLEUSDT','JUPUSDT','RUNEUSDT',
    'PEPEUSDT','WIFUSDT','BONKUSDT','FLOKIUSDT','SHIBUSDT',
    'ARBUSDT','OPUSDT','GMXUSDT','DYDXUSDT','BLURUSDT',
    'ENSUSDT','CAKEUSDT','XLMUSDT','TRXUSDT','TONUSDT',
    'SOLUSDT','BNBUSDT','XRPUSDT','ADAUSDT','AVAXUSDT',
    'DOGEUSDT','DOTUSDT','LINKUSDT','LTCUSDT','ATOMUSDT',
]

SL           = -0.08   # 止損 -8%
TP           =  0.25   # 止盈 +25%
MAX_HOLD     = 5       # 最多持倉天數
VOL_THRESH   = 2.5     # 量爆增門檻（倍均量）
OI_THRESH    = 0.10    # OI暴增門檻（10%）

async def fetch(url: str):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url, headers={"User-Agent": "Backtest/1.0"})
        return r.json() if r.status_code == 200 else []

async def get_klines(sym, limit=60):
    return await fetch(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1d&limit={limit}")

async def get_oi(sym, limit=30):
    return await fetch(f"https://fapi.binance.com/futures/data/openInterestHist?symbol={sym}&period=1d&limit={limit}")

def find_anomalies(klines, oi_hist):
    if len(klines) < 25:
        return []

    # Build OI change map by open_time
    oi_chg_map = {}
    for i in range(1, len(oi_hist)):
        try:
            prev = float(oi_hist[i-1]['sumOpenInterest'])
            curr = float(oi_hist[i]['sumOpenInterest'])
            chg  = (curr - prev) / prev if prev > 0 else 0
            oi_chg_map[int(oi_hist[i]['timestamp'])] = chg
        except Exception:
            pass

    anomalies = []
    for i in range(20, len(klines)):
        # 20-day average quote volume (excluding today)
        avg_vol = sum(float(klines[j][7]) for j in range(i-20, i)) / 20
        today_vol = float(klines[i][7])
        vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1

        if vol_ratio < VOL_THRESH:
            continue

        open_time = int(klines[i][0])
        oi_chg    = oi_chg_map.get(open_time, None)
        confirmed = oi_chg is not None and oi_chg >= OI_THRESH
        price_up  = float(klines[i][4]) >= float(klines[i][1])  # close >= open

        anomalies.append({
            'day_idx':   i,
            'vol_ratio': vol_ratio,
            'oi_chg':    oi_chg,
            'confirmed': confirmed,
            'direction': 'long' if price_up else 'short',
        })
    return anomalies

def sim_trade(klines, entry_idx, direction):
    if entry_idx >= len(klines):
        return None

    entry = float(klines[entry_idx][1])  # next-day open
    if entry <= 0:
        return None

    mult = 1 if direction == 'long' else -1

    for j in range(entry_idx, min(entry_idx + MAX_HOLD, len(klines))):
        hi   = float(klines[j][2])
        lo   = float(klines[j][3])
        days = j - entry_idx + 1

        # Check SL/TP on intraday range (pessimistic: SL checked before TP)
        ret_lo = (lo - entry) / entry * mult
        ret_hi = (hi - entry) / entry * mult

        if ret_lo <= SL:
            return {'ret': SL, 'days': days, 'reason': '止損'}
        if ret_hi >= TP:
            return {'ret': TP, 'days': days, 'reason': '止盈'}

    # Max hold: use closing price of last bar
    close = float(klines[min(entry_idx + MAX_HOLD - 1, len(klines)-1)][4])
    ret   = (close - entry) / entry * mult
    return {'ret': ret, 'days': MAX_HOLD, 'reason': f'到期{MAX_HOLD}天'}

def print_stats(trades, label):
    if not trades:
        print(f"  【{label}】無信號")
        return
    wins   = [t for t in trades if t['ret'] > 0]
    losses = [t for t in trades if t['ret'] <= 0]
    avg    = sum(t['ret'] for t in trades) / len(trades) * 100
    wr     = len(wins) / len(trades) * 100
    aw     = sum(t['ret'] for t in wins)   / len(wins)   * 100 if wins   else 0
    al     = sum(t['ret'] for t in losses) / len(losses) * 100 if losses else 0
    pf     = abs(aw / al) if al != 0 else float('inf')

    print(f"  【{label}】共 {len(trades)} 筆")
    print(f"    勝率:     {wr:.0f}%  ({len(wins)}勝 / {len(losses)}敗)")
    print(f"    平均報酬: {avg:+.2f}%")
    print(f"    平均獲利: {aw:+.2f}%  |  平均虧損: {al:+.2f}%")
    if al < 0:
        print(f"    獲利因子: {pf:.2f}x")

async def run():
    print("\n" + "═"*64)
    print("  異常信號回測 │ 量爆增 + OI暴增 → 暴漲偵測")
    print(f"  止損 {SL*100:.0f}%  止盈 {TP*100:.0f}%  最長持倉 {MAX_HOLD} 天")
    print(f"  量爆增門檻 {VOL_THRESH}x均量  OI確認門檻 {OI_THRESH*100:.0f}%")
    print("═"*64)
    print("\n📡 載入數據中...\n")

    all_trades      = []
    vol_only        = []
    vol_oi_trades   = []
    coin_stats      = []  # (symbol, signal_count, win_rate)

    for sym in COINS:
        klines = await get_klines(sym, 60)
        await asyncio.sleep(0.1)
        oi     = await get_oi(sym, 30)
        await asyncio.sleep(0.1)

        if len(klines) < 25:
            continue

        anomalies = find_anomalies(klines, oi)
        sym_trades = []

        for a in anomalies:
            if a['direction'] != 'long':  # 只做多（暴漲偵測）
                continue
            t = sim_trade(klines, a['day_idx'] + 1, 'long')
            if t is None:
                continue
            t.update({'symbol': sym[:-4], 'vol_ratio': a['vol_ratio'],
                      'oi_chg': a['oi_chg'], 'confirmed': a['confirmed']})
            all_trades.append(t)
            sym_trades.append(t)
            if a['confirmed']:
                vol_oi_trades.append(t)
            else:
                vol_only.append(t)

        if sym_trades:
            wr = sum(1 for t in sym_trades if t['ret'] > 0) / len(sym_trades) * 100
            coin_stats.append((sym[:-4], len(sym_trades), wr))

    print(f"  ✓ 分析 {len(COINS)} 幣種，找到 {len(all_trades)} 個多頭信號\n")
    print("═"*64)
    print("  📊 回測結果")
    print("═"*64)

    print_stats(all_trades,    "所有量爆增信號")
    print()
    print_stats(vol_only,      "純量爆增（無 OI 確認）")
    print()
    print_stats(vol_oi_trades, "量爆增 + OI≥10%（雙重確認）")

    # Top 5 trades
    if all_trades:
        print(f"\n{'─'*64}")
        print("  🏆 最佳交易（前5筆）")
        top5 = sorted(all_trades, key=lambda x: x['ret'], reverse=True)[:5]
        for t in top5:
            oi_s = f"+{t['oi_chg']*100:.1f}% OI" if t['oi_chg'] is not None else "OI無數據"
            print(f"    {t['symbol']:<8}  量×{t['vol_ratio']:.1f}  {oi_s}  "
                  f"→ {t['ret']*100:+.1f}%  [{t['reason']} / 第{t['days']}天]")

    # Top winning coins
    if coin_stats:
        print(f"\n{'─'*64}")
        print("  📈 信號最多幣種（前10）")
        for sym, cnt, wr in sorted(coin_stats, key=lambda x: -x[1])[:10]:
            print(f"    {sym:<8}  {cnt} 筆信號  勝率 {wr:.0f}%")

    print("═"*64)
    print("\n⚠️  歷史回測不代表未來績效。未計入手續費。\n")

asyncio.run(run())
