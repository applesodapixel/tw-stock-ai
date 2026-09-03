import json, urllib.request, urllib.parse, time, math
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)
TODAY = NOW.date()

TRACKED_SECTORS = [
    {
        "name": "EMS 電子代工",
        "state": "觀察",
        "related": ["AI 伺服器","伺服器 ODM","PCB","散熱","電源供應","網通"],
        "stocks": [
            ("2317","鴻海","TWSE"),("2354","鴻準","TWSE"),("2382","廣達","TWSE"),
            ("4938","和碩","TWSE"),("3231","緯創","TWSE"),("2356","英業達","TWSE")
        ]
    },
    {
        "name": "銀行金融",
        "state": "觀察",
        "related": ["金控","銀行","證券","壽險"],
        "stocks": [
            ("2881","富邦金","TWSE"),("2882","國泰金","TWSE"),("2884","玉山金","TWSE"),
            ("2886","兆豐金","TWSE"),("2891","中信金","TWSE"),("5880","合庫金","TWSE")
        ]
    },
    {
        "name": "記憶體",
        "state": "觀察",
        "related": ["DRAM","NAND Flash","HBM","記憶體模組"],
        "stocks": [
            ("2408","南亞科","TWSE"),("2344","華邦電","TWSE"),("2337","旺宏","TWSE"),
            ("8299","群聯","TPEx")
        ]
    }
]

def get_json(url, timeout=35):
    req = urllib.request.Request(url, headers={
        "User-Agent":"Mozilla/5.0 tw-stock-ai/0.8",
        "Accept":"application/json"
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8-sig"))

def load(name, default):
    p = DATA / name
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default

def save(name, obj):
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def num(v):
    if v is None: return None
    s = str(v).strip().replace(",","").replace("+","")
    if s in ("","--","---","-","N/A","null"): return None
    try: return float(s)
    except Exception: return None

def roc_to_iso(s):
    a = str(s).strip().split("/")
    return f"{int(a[0])+1911:04d}-{int(a[1]):02d}-{int(a[2]):02d}"

def next_weekday(d):
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n

def pick(d, names):
    if not isinstance(d, dict): return None
    for n in names:
        if n in d: return d[n]
    # fuzzy fallback
    for k,v in d.items():
        ks = str(k)
        if any(n in ks for n in names):
            return v
    return None

# ---------- 大盤 60 日 ----------
def fetch_fmtqik(d):
    ds = d.strftime("%Y%m%d")
    j = get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={ds}&response=json")
    out = []
    for r in (j.get("data") or []):
        if len(r) >= 5:
            out.append({"date":roc_to_iso(r[0]), "close":num(r[4])})
    return out

def fetch_bfi(d):
    ds = d.strftime("%Y%m%d")
    j = get_json(f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?date={ds}&response=json")
    vals = {"foreign":0.0,"trust":0.0,"dealer":0.0}
    for r in (j.get("data") or []):
        if len(r) < 4: continue
        name = str(r[0]); v = num(r[-1]) or 0
        if "外資" in name: vals["foreign"] += v
        elif "投信" in name: vals["trust"] += v
        elif "自營商" in name: vals["dealer"] += v
    for k in vals:
        vals[k] = round(vals[k] / 100_000_000, 2)
    vals["total"] = round(sum(vals.values()), 2)
    return vals

def market_backfill():
    rows = {}
    cursor = TODAY.replace(day=1)
    for _ in range(5):
        try:
            for x in fetch_fmtqik(cursor):
                if x["date"] <= TODAY.isoformat():
                    rows[x["date"]] = x
        except Exception as e:
            print("FMTQIK skip", cursor, e)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
        time.sleep(.2)

    dates = sorted(rows)[-60:]
    old = load("market_60d.json", {}).get("days", [])
    oldmap = {x["date"]:x for x in old}
    days = []
    for ds in dates:
        x = rows[ds]
        prev = oldmap.get(ds, {})
        for k in ("foreign","trust","dealer","total"):
            x[k] = prev.get(k)
        days.append(x)

    # 每次只補最近缺少的 15 日法人，避免 Actions 太慢
    missing = [x for x in days if x.get("total") is None][-15:]
    for x in missing:
        try:
            x.update(fetch_bfi(datetime.fromisoformat(x["date"]).date()))
        except Exception as e:
            print("BFI skip", x["date"], e)
        time.sleep(.2)

    prev = None
    for x in days:
        c = x.get("close")
        x["return"] = None if prev in (None,0) or c is None else round((c/prev-1)*100,2)
        prev = c

    save("market_60d.json", {"days":days})
    save("market_5d.json", {"days":days[-5:]})
    return days

# ---------- 當日個股行情 ----------
def twse_daily_quotes():
    try:
        rows = get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    except Exception as e:
        print("TWSE STOCK_DAY_ALL failed", e); return {}
    out = {}
    for d in rows if isinstance(rows,list) else []:
        code = str(pick(d,["Code","證券代號","股票代號"]) or "").strip()
        if not code: continue
        close = num(pick(d,["ClosingPrice","收盤價","收盤"]))
        change = num(pick(d,["Change","漲跌價差"]))
        prev = None if close is None or change is None else close - change
        ret = None if prev in (None,0) else (change/prev*100)
        out[code] = {
            "close": close,
            "ret1": None if ret is None else round(ret,2),
            "name": str(pick(d,["Name","證券名稱","股票名稱"]) or "")
        }
    return out

def tpex_daily_quotes():
    urls = [
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes",
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
    ]
    rows = None
    for u in urls:
        try:
            rows = get_json(u); break
        except Exception as e:
            print("TPEx quotes fallback", e)
    out = {}
    for d in rows if isinstance(rows,list) else []:
        code = str(pick(d,["SecuritiesCompanyCode","SecuritiesCode","股票代號","證券代號","Code"]) or "").strip()
        if not code: continue
        close = num(pick(d,["Close","ClosingPrice","收盤價","收盤"]))
        pct = num(pick(d,["ChangePercent","漲跌幅","漲跌幅%"]))
        out[code] = {
            "close": close,
            "ret1": pct,
            "name": str(pick(d,["CompanyName","SecuritiesCompanyName","證券名稱","股票名稱","Name"]) or "")
        }
    return out

# ---------- 三大法人個股 ----------
def parse_twse_t86(d):
    ds = d.strftime("%Y%m%d")
    j = get_json(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={ds}&selectType=ALLBUT0999&response=json")
    fields = j.get("fields") or []
    rows = j.get("data") or []
    out = {}
    idx = {str(f):i for i,f in enumerate(fields)}
    def find_idx(tokens):
        for f,i in idx.items():
            if any(t in f for t in tokens): return i
        return None
    i_code = find_idx(["證券代號"])
    i_total = find_idx(["三大法人買賣超股數"])
    if i_total is None:
        # 合計買賣超股數
        i_total = find_idx(["買賣超股數"])
    for r in rows:
        try:
            code = str(r[i_code]).strip() if i_code is not None else str(r[0]).strip()
            shares = num(r[i_total]) if i_total is not None else None
            if code and shares is not None:
                out[code] = shares
        except Exception:
            continue
    return out

def parse_tpex_insti():
    urls = [
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_3insti_trading",
        "https://www.tpex.org.tw/openapi/v1/tpex_3insti_trading"
    ]
    rows = None
    for u in urls:
        try:
            rows = get_json(u); break
        except Exception as e:
            print("TPEx insti fallback", e)
    out = {}
    for d in rows if isinstance(rows,list) else []:
        code = str(pick(d,["SecuritiesCompanyCode","SecuritiesCode","證券代號","股票代號","Code"]) or "").strip()
        total = num(pick(d,["TotalNetBuySell","ThreeInstitutionalInvestorsNetBuySell","三大法人買賣超股數","合計買賣超股數","NetBuySell"]))
        if code and total is not None:
            out[code] = total
    return out

# ---------- 個股價格歷史 ----------
def twse_stock_month(code, d):
    ds = d.strftime("%Y%m%d")
    j = get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?stockNo={code}&date={ds}&response=json")
    fields = j.get("fields") or []
    rows = j.get("data") or []
    fi = {str(f):i for i,f in enumerate(fields)}
    date_i = next((i for f,i in fi.items() if "日期" in f),0)
    close_i = next((i for f,i in fi.items() if "收盤價" in f),6 if fields else 6)
    out = []
    for r in rows:
        try:
            out.append({"date":roc_to_iso(r[date_i]), "close":num(r[close_i])})
        except Exception:
            continue
    return out

def stock_price_history(code):
    # 取最近 3 個月份，足夠計算 20 日
    rows = {}
    cursor = TODAY.replace(day=1)
    for _ in range(3):
        try:
            for x in twse_stock_month(code,cursor):
                rows[x["date"]] = x["close"]
        except Exception as e:
            print("STOCK_DAY skip",code,cursor,e)
        cursor = (cursor-timedelta(days=1)).replace(day=1)
        time.sleep(.15)
    return [{"date":d,"close":rows[d]} for d in sorted(rows)]

# ---------- 法人歷史：只追蹤目前板塊 ----------
def backfill_stock_institutional(market_days):
    hist = load("stock_institutional_20d.json", {"days":[]})
    bydate = {x["date"]:x for x in hist.get("days",[])}
    target_dates = [x["date"] for x in market_days[-20:]]
    missing = [d for d in target_dates if d not in bydate][-8:]  # 每輪最多補 8 日
    for ds in missing:
        try:
            m = parse_twse_t86(datetime.fromisoformat(ds).date())
            bydate[ds] = {"date":ds,"twse":m}
        except Exception as e:
            print("T86 backfill skip",ds,e)
        time.sleep(.2)
    days = [bydate[d] for d in sorted(bydate) if d in target_dates]
    save("stock_institutional_20d.json", {"days":days})
    return days

def shares_to_yi(shares, close):
    if shares is None or close is None: return None
    return round(shares * close / 100_000_000, 2)

def pct(a,b):
    if a in (None,0) or b is None: return None
    return round((b/a-1)*100,2)

def score_stock(ret1, mom5, mom20, insti_yi):
    # 透明基準分數 0~100
    vals = [
        50 if ret1 is None else 50 + max(-15,min(15,ret1*4)),
        50 if mom5 is None else 50 + max(-15,min(15,mom5*2)),
        50 if mom20 is None else 50 + max(-12,min(12,mom20)),
        50 if insti_yi is None else 50 + max(-18,min(18,insti_yi/2))
    ]
    s = round(sum(vals)/len(vals))
    signal = "偏多" if s>=62 else "偏弱" if s<=38 else "觀望"
    return s, signal

def build_stocks_and_sectors(market_days):
    twq = twse_daily_quotes()
    tpq = tpex_daily_quotes()
    try:
        tw_insti_today = parse_twse_t86(TODAY)
    except Exception as e:
        print("T86 today failed",e); tw_insti_today = {}
    tp_insti_today = parse_tpex_insti()
    inst_hist = backfill_stock_institutional(market_days)

    all_stocks = {}
    sectors_out = []

    for sec in TRACKED_SECTORS:
        members = []
        for code,name,market in sec["stocks"]:
            q = (twq if market=="TWSE" else tpq).get(code,{})
            close = q.get("close")
            ret1 = q.get("ret1")
            price_hist = []
            if market=="TWSE":
                price_hist = stock_price_history(code)
                if close is None and price_hist:
                    close = price_hist[-1]["close"]
                cvals = [x["close"] for x in price_hist if x["close"] is not None]
                mom5 = pct(cvals[-6], cvals[-1]) if len(cvals)>=6 else None
                mom20 = pct(cvals[-21], cvals[-1]) if len(cvals)>=21 else None
            else:
                mom5 = mom20 = None

            shares = (tw_insti_today if market=="TWSE" else tp_insti_today).get(code)
            insti_yi = shares_to_yi(shares, close)
            score, signal = score_stock(ret1,mom5,mom20,insti_yi)

            obj = {
                "code":code,"name":name,"market":market,"close":close,"ret1":ret1,
                "mom5":mom5,"mom20":mom20,"insti_yi":insti_yi,"score":score,"signal":signal
            }
            members.append(obj)
            all_stocks[code] = obj

        # 當日法人板塊合計
        known = [x["insti_yi"] for x in members if x.get("insti_yi") is not None]
        day_net = round(sum(known),2) if known else None

        # 5日/20日：以 TWSE T86 回補資料估算
        def hist_sum(n):
            total = 0.0; count = 0
            for day in inst_hist[-n:]:
                m = day.get("twse",{})
                for x in members:
                    if x["market"]!="TWSE": continue
                    sh = m.get(x["code"])
                    if sh is not None and x.get("close") is not None:
                        total += sh*x["close"]/100_000_000
                        count += 1
            return round(total,2) if count else None

        net5, net20 = hist_sum(5), hist_sum(20)
        rets5 = [x["mom5"] for x in members if x.get("mom5") is not None]
        ret5 = round(sum(rets5)/len(rets5),2) if rets5 else None

        if day_net is None: state="資料建立中"
        elif day_net > 10: state="法人偏多"
        elif day_net < -10: state="法人偏空"
        else: state="資金分歧"

        sectors_out.append({
            "name":sec["name"],"state":state,
            "net_buy_value":day_net,
            "net_buy_text":"待資料" if day_net is None else f"{day_net:+.1f} 億",
            "net5_value":net5,
            "net5_text":"資料建立中" if net5 is None else f"{net5:+.1f} 億",
            "net20_value":net20,
            "net20_text":"資料建立中" if net20 is None else f"{net20:+.1f} 億",
            "ret5_value":ret5,
            "ret5_text":"資料建立中" if ret5 is None else f"{ret5:+.2f}%",
            "note":"板塊數值以目前追蹤成分股彙總；不是整個產業所有股票。",
            "stocks":members,"related":sec["related"]
        })
    return list(all_stocks.values()), sectors_out

# ---------- 預測 / 驗證 / 自我調整 ----------
def default_model():
    return {
        "generation":0,
        "version":"baseline-0.8",
        "weights":{"mom5":0.55,"flow5":0.45},
        "min_verified_for_tuning":20,
        "last_tuned_at":None
    }

def model_score(features, weights):
    return weights["mom5"]*features["mom5"] + weights["flow5"]*(features["flow5"]/300.0)

def direction_from_score(score):
    return "偏多" if score>.35 else "偏空" if score<-.35 else "震盪"

def tune_model(preds, model):
    verified = [p for p in preds if p.get("verified") and p.get("inputs_snapshot")]
    if len(verified) < model.get("min_verified_for_tuning",20):
        return model

    current = model.get("weights",{"mom5":.55,"flow5":.45})
    best = (0, current)
    # 簡單且透明的網格搜尋；只使用當時已保存的 feature snapshot
    for wm in [x/10 for x in range(2,9)]:
        wf = 1-wm
        ok=0
        for p in verified:
            f=p["inputs_snapshot"]
            pred=direction_from_score(model_score(f,{"mom5":wm,"flow5":wf}))
            ok += int(pred==p.get("actual_direction"))
        acc=ok/len(verified)
        if acc>best[0]:
            best=(acc,{"mom5":wm,"flow5":wf})
    old_acc=0
    for p in verified:
        pred=direction_from_score(model_score(p["inputs_snapshot"],current))
        old_acc += int(pred==p.get("actual_direction"))
    old_acc/=len(verified)

    if best[0] > old_acc + 0.03 and best[1] != current:
        model["generation"] = int(model.get("generation",0))+1
        model["weights"] = best[1]
        model["version"] = f"adaptive-0.8-g{model['generation']}"
        model["last_tuned_at"] = NOW.isoformat(timespec="seconds")
        model["last_tuning_accuracy"] = round(best[0],3)
    return model

def make_forecast(days, model):
    usable = [x for x in days if x.get("return") is not None and x.get("total") is not None]
    if len(usable)<20: return None
    last = usable[-1]
    features = {
        "mom5": round(sum(x["return"] for x in usable[-5:]),2),
        "flow5": round(sum(x["total"] for x in usable[-5:]),2)
    }
    score = model_score(features, model["weights"])
    direction = direction_from_score(score)
    confidence = round(min(80,max(52,52+abs(score)*6)))
    rets=[x["return"] for x in usable[-20:]]
    vol=(sum((r-sum(rets)/len(rets))**2 for r in rets)/len(rets))**0.5 if rets else None
    range_text = "—" if vol is None else f"約 ±{vol:.2f}%"
    return {
        "prediction_for":next_weekday(datetime.fromisoformat(last["date"]).date()).isoformat(),
        "made_at":NOW.isoformat(timespec="seconds"),
        "direction":direction,"confidence":confidence,"range":range_text,
        "reason":f"近5日指數動能 {features['mom5']:+.2f}%，三大法人合計 {features['flow5']:+.2f} 億；模型權重 動能 {model['weights']['mom5']:.0%} / 法人 {model['weights']['flow5']:.0%}。",
        "model_version":model["version"],
        "model_generation":model["generation"],
        "inputs_snapshot":features
    }

def verify_predictions(days, preds):
    bydate={x["date"]:x for x in days}
    for p in preds:
        if p.get("verified"): continue
        real=bydate.get(p.get("prediction_for"))
        if not real or real.get("return") is None: continue
        rr=real["return"]
        actual="偏多" if rr>.2 else "偏空" if rr<-.2 else "震盪"
        p.update({"verified":True,"actual_return":rr,"actual_direction":actual,"correct":actual==p.get("direction")})
        if p["correct"]:
            p["error_reason"]="方向命中。"
        else:
            # 僅陳述可觀測變化
            snap=p.get("inputs_snapshot") or {}
            flow_then=snap.get("flow5")
            flow_now=sum(x.get("total") or 0 for x in days[-5:])
            factors=[]
            if flow_then is not None and flow_then*flow_now<0: factors.append("近5日法人資金方向發生反轉")
            if abs(rr)>=2: factors.append("實際單日波動明顯放大")
            if not factors: factors.append("市場走勢未延續預測當下的短期動能/資金訊號")
            p["error_reason"]="；".join(factors)+"。"
    return preds

def update_latest(days, forecast, preds, model, stocks, sectors):
    latest=load("latest.json",{})
    if days:
        last=days[-1]
        latest["market_date"]=last["date"]
        latest["market_return"]=last.get("return")
        latest["history_days"]=len(days)
    latest["forecast"]=forecast
    latest["model"]=model
    latest["stocks"]=stocks
    latest["sectors"]=sectors
    latest["updated_at"]=NOW.isoformat(timespec="seconds")

    verified=[p for p in preds if p.get("verified")]
    if verified:
        p=verified[-1]
        latest["verification"]={"html":f"<b>{'命中' if p['correct'] else '未命中'}</b><br>預測 {p['direction']}，實際 {p['actual_direction']}（{p['actual_return']:+.2f}%）。{p['error_reason']}"}
    else:
        latest["verification"]={"html":"<b>尚無可驗證的正式預測。</b><br>第一筆事前預測已保存，下一交易日收盤後會自動核對。"}

    latest["summary"]=(
        f"資料已自動更新至 {latest.get('market_date','—')}。"
        + (f" 明日正式預測：{forecast['direction']}，信心 {forecast['confidence']}%。" if forecast else " 正式預測資料不足。")
        + " 板塊與個股資料以官方盤後資料為主；缺值會保留為待資料，不以 0 代替。"
    )
    save("latest.json",latest)
    save("sectors.json",{"sectors":sectors})
    save("model.json",model)

def main():
    days=market_backfill()
    stocks,sectors=build_stocks_and_sectors(days)

    pred_obj=load("predictions.json",{"predictions":[]})
    if isinstance(pred_obj,list): pred_obj={"predictions":pred_obj}
    preds=verify_predictions(days,pred_obj.get("predictions",[]))

    model=load("model.json",default_model())
    if "weights" not in model: model=default_model()
    model=tune_model(preds,model)

    fc=make_forecast(days,model)
    if fc and not any(p.get("prediction_for")==fc["prediction_for"] for p in preds):
        preds.append(fc)

    save("predictions.json",{"predictions":preds[-300:]})
    update_latest(days,fc,preds,model,stocks,sectors)
    save("backfill_meta.json",{
        "updated_at":NOW.isoformat(timespec="seconds"),
        "market_days":len(days),
        "target_market_days":60,
        "stock_detail_count":len(stocks),
        "sector_count":len(sectors),
        "engine":"v0.8"
    })
    print("v0.8 updated",len(days),"market days",len(stocks),"stocks",fc)

if __name__=="__main__":
    main()
