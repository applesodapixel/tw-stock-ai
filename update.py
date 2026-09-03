import json, urllib.request, urllib.parse, time
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT=Path(__file__).resolve().parent
DATA=ROOT/"data"; DATA.mkdir(exist_ok=True)
TZ=timezone(timedelta(hours=8))
TODAY=datetime.now(TZ).date()

def get_json(url, timeout=30):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 tw-stock-ai/0.7","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8-sig"))

def num(x):
    if x is None:return None
    s=str(x).replace(",","").replace("+","").strip()
    try:return float(s)
    except:return None

def save(name,obj):
    (DATA/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding="utf-8")

def load(name, default):
    p=DATA/name
    try:return json.loads(p.read_text(encoding="utf-8"))
    except:return default

def roc_to_iso(s):
    # 115/09/03
    a=s.strip().split("/")
    return f"{int(a[0])+1911:04d}-{int(a[1]):02d}-{int(a[2]):02d}"

def fetch_fmtqik(d):
    ds=d.strftime("%Y%m%d")
    j=get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={ds}&response=json")
    rows=j.get("data") or []
    ans=[]
    for r in rows:
        if len(r)<6:continue
        ans.append({"date":roc_to_iso(r[0]),"close":num(r[4])})
    return ans

def fetch_bfi(d):
    ds=d.strftime("%Y%m%d")
    j=get_json(f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?date={ds}&response=json")
    rows=j.get("data") or []
    vals={"foreign":0.0,"trust":0.0,"dealer":0.0}
    for r in rows:
        if len(r)<4:continue
        name=str(r[0]); v=num(r[-1]) or 0
        if "外資" in name: vals["foreign"]+=v
        elif "投信" in name: vals["trust"]+=v
        elif "自營商" in name: vals["dealer"]+=v
    # NTD -> 億
    for k in vals: vals[k]=round(vals[k]/100_000_000,2)
    vals["total"]=round(sum(vals.values()),2)
    return vals

def market_backfill():
    # FMTQIK returns a month; fetch enough months to obtain >=60 trading days.
    rows={}
    cursor=TODAY.replace(day=1)
    for _ in range(5):
        for x in fetch_fmtqik(cursor):
            if x["date"]<=TODAY.isoformat(): rows[x["date"]]=x
        cursor=(cursor-timedelta(days=1)).replace(day=1)
        time.sleep(.25)
    dates=sorted(rows)[-60:]
    old=load("market_60d.json",{}).get("days",[])
    oldmap={x["date"]:x for x in old}
    days=[]
    for ds in dates:
        x=rows[ds]
        prev=oldmap.get(ds,{})
        x.update({k:prev.get(k) for k in ("foreign","trust","dealer","total")})
        days.append(x)
    # Fill institutional totals only for recent missing days, max 15/request run.
    missing=[x for x in days if x.get("total") is None][-15:]
    for x in missing:
        try:
            f=fetch_bfi(datetime.fromisoformat(x["date"]).date())
            x.update(f); time.sleep(.25)
        except Exception as e:
            print("BFI skip",x["date"],e)
    # returns
    prev=None
    for x in days:
        x["return"]=None if prev in (None,0) else round((x["close"]/prev-1)*100,2)
        prev=x["close"]
    save("market_60d.json",{"days":days})
    save("market_5d.json",{"days":days[-5:]})
    return days

def next_weekday(d):
    n=d+timedelta(days=1)
    while n.weekday()>=5:n+=timedelta(days=1)
    return n

def make_forecast(days):
    usable=[x for x in days if x.get("return") is not None and x.get("total") is not None]
    if len(usable)<20:return None
    last=usable[-1]
    r5=sum(x["return"] for x in usable[-5:])
    flow5=sum(x["total"] for x in usable[-5:])
    score=.55*r5 + .45*(flow5/300)
    direction="偏多" if score>.35 else "偏空" if score<-.35 else "震盪"
    confidence=min(78,max(52,52+abs(score)*6))
    return {
      "prediction_for":next_weekday(datetime.fromisoformat(last["date"]).date()).isoformat(),
      "made_at":datetime.now(TZ).isoformat(timespec="seconds"),
      "direction":direction,"confidence":round(confidence),
      "range":"待累積波動模型",
      "reason":f"近5日指數動能 {r5:+.2f}%，三大法人合計 {flow5:+.2f} 億；v0.7 先以透明權重產生基準預測。",
      "model_version":"baseline-0.7"
    }

def verify_predictions(days,preds):
    bydate={x["date"]:x for x in days}
    for p in preds:
        if p.get("verified"):continue
        real=bydate.get(p.get("prediction_for"))
        if not real or real.get("return") is None:continue
        rr=real["return"]; actual="偏多" if rr>.2 else "偏空" if rr<-.2 else "震盪"
        p["verified"]=True;p["actual_return"]=rr;p["actual_direction"]=actual;p["correct"]=actual==p.get("direction")
        if not p["correct"]:
            p["error_reason"]="實際收盤方向與事前預測不同。優先檢查法人資金是否反轉、隔夜事件與族群輪動；目前版本不把無法觀測的原因寫成確定事實。"
        else:p["error_reason"]="方向命中。"
    return preds

def update_latest(days, forecast, preds):
    latest=load("latest.json",{})
    if days:
        last=days[-1]; latest["market_date"]=last["date"];latest["market_return"]=last.get("return")
        latest["history_days"]=len(days)
    latest["forecast"]=forecast
    latest["model"]=latest.get("model") or {"generation":0}
    latest["updated_at"]=datetime.now(TZ).isoformat(timespec="seconds")
    verified=[p for p in preds if p.get("verified")]
    if verified:
        p=verified[-1]
        latest["verification"]={"html":f"{'命中' if p['correct'] else '未命中'}：預測 {p['direction']}，實際 {p['actual_direction']}（{p['actual_return']:+.2f}%）。{p['error_reason']}"}
    latest["summary"]="資料已由 GitHub Actions 自動更新。正式預測只使用預測當下已知資料；回補歷史不會偽裝成事前預測。"
    save("latest.json",latest)

def main():
    days=market_backfill()
    preds=load("predictions.json",{"predictions":[]})
    if isinstance(preds,list):preds={"predictions":preds}
    arr=verify_predictions(days,preds.get("predictions",[]))
    fc=make_forecast(days)
    if fc and not any(p.get("prediction_for")==fc["prediction_for"] for p in arr):
        arr.append(fc)
    save("predictions.json",{"predictions":arr[-200:]})
    update_latest(days,fc,arr)
    save("backfill_meta.json",{"updated_at":datetime.now(TZ).isoformat(timespec="seconds"),"market_days":len(days),"target_market_days":60,"engine":"v0.7"})
    print("updated",len(days),"days",fc)

if __name__=="__main__": main()
