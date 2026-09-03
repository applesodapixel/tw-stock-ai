import json, urllib.request, urllib.parse, urllib.error, time, math, os, csv, io, re
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)
TODAY = NOW.date()
ENGINE = "v1.0"
INDUSTRY_MAP = {
    "01":"水泥工業","02":"食品工業","03":"塑膠工業","04":"紡織纖維","05":"電機機械",
    "06":"電器電纜","08":"玻璃陶瓷","09":"造紙工業","10":"鋼鐵工業","11":"橡膠工業",
    "12":"汽車工業","14":"建材營造","15":"航運業","16":"觀光餐旅","17":"金融保險",
    "18":"貿易百貨","19":"綜合","20":"其他","21":"化學工業","22":"生技醫療業",
    "23":"油電燃氣業","24":"半導體業","25":"電腦及週邊設備業","26":"光電業",
    "27":"通信網路業","28":"電子零組件業","29":"電子通路業","30":"資訊服務業",
    "31":"其他電子業","32":"文化創意業","33":"農業科技業","35":"綠能環保",
    "36":"數位雲端","37":"運動休閒","38":"居家生活","80":"管理股票"
}

def industry_zh(v):
    s=str(v or "").strip()
    if not s:return "其他"
    # 官方 API 有時回傳 01、1、01 水泥工業；全部正規化為中文。
    m=re.match(r"^(\d{1,2})(?:\D.*)?$",s)
    if m:
        key=m.group(1).zfill(2)
        return INDUSTRY_MAP.get(key, s)
    return INDUSTRY_MAP.get(s.zfill(2) if s.isdigit() else s, s)

def is_common_stock_code(code):
    return len(code)==4 and code.isdigit() and not code.startswith("0")


def get_json(url, timeout=40):
    headers={"User-Agent":"Mozilla/5.0 tw-stock-ai/1.0","Accept":"application/json,text/plain,*/*","Referer":"https://www.twse.com.tw/"}
    current=url
    for _ in range(4):
        req=urllib.request.Request(current,headers=headers,method="GET")
        try:
            with urllib.request.urlopen(req,timeout=timeout) as r:
                body=r.read().decode("utf-8-sig").strip()
                if not body: raise ValueError("empty response")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code in (301,302,303,307,308) and e.headers.get("Location"):
                current=urllib.parse.urljoin(current,e.headers["Location"]); continue
            raise
    raise RuntimeError("too many redirects")

def load(name, default):
    try: return json.loads((DATA/name).read_text(encoding="utf-8"))
    except Exception: return default

def save(name,obj):
    (DATA/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding="utf-8")

def num(v):
    if v is None:return None
    s=str(v).strip().replace(",","").replace("+","").replace("%","")
    if s in ("","--","---","-","N/A","null"):return None
    try:return float(s)
    except:return None

def pick(d,names):
    if not isinstance(d,dict):return None
    for n in names:
        if n in d:return d[n]
    for k,v in d.items():
        ks=str(k)
        if any(n in ks for n in names):return v
    return None

def roc_to_iso(s):
    a=str(s).strip().split("/")
    return f"{int(a[0])+1911:04d}-{int(a[1]):02d}-{int(a[2]):02d}"

def next_weekday(d):
    n=d+timedelta(days=1)
    while n.weekday()>=5:n+=timedelta(days=1)
    return n

# ---------- 大盤 ----------
def fetch_fmtqik(d):
    ds=d.strftime("%Y%m%d")
    j=get_json(f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={ds}&response=json")
    out=[]
    for r in j.get("data") or []:
        if len(r)>=5: out.append({"date":roc_to_iso(r[0]),"close":num(r[4])})
    return out

def fetch_bfi(d):
    ds=d.strftime("%Y%m%d")
    j=get_json(f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?date={ds}&response=json")
    vals={"foreign":0.0,"trust":0.0,"dealer":0.0}
    for r in j.get("data") or []:
        if len(r)<4:continue
        name=str(r[0]); v=num(r[-1]) or 0
        if "外資" in name:vals["foreign"]+=v
        elif "投信" in name:vals["trust"]+=v
        elif "自營商" in name:vals["dealer"]+=v
    for k in vals:vals[k]=round(vals[k]/100_000_000,2)
    vals["total"]=round(sum(vals.values()),2)
    return vals

def market_backfill():
    rows={}; cursor=TODAY.replace(day=1)
    for _ in range(5):
        try:
            for x in fetch_fmtqik(cursor):
                if x["date"]<=TODAY.isoformat():rows[x["date"]]=x
        except Exception as e:print("FMTQIK",cursor,e)
        cursor=(cursor-timedelta(days=1)).replace(day=1);time.sleep(.12)
    dates=sorted(rows)[-60:]
    old=load("market_60d.json",{}).get("days",[])
    oldmap={x["date"]:x for x in old}
    days=[]
    for ds in dates:
        x=rows[ds]; prev=oldmap.get(ds,{})
        for k in ("foreign","trust","dealer","total"):x[k]=prev.get(k)
        days.append(x)
    for x in [z for z in days if z.get("total") is None][-15:]:
        try:x.update(fetch_bfi(datetime.fromisoformat(x["date"]).date()))
        except Exception as e:print("BFI",x["date"],e)
        time.sleep(.1)
    prev=None
    for x in days:
        c=x.get("close");x["return"]=None if prev in (None,0) or c is None else round((c/prev-1)*100,2);prev=c
    save("market_60d.json",{"days":days});save("market_5d.json",{"days":days[-5:]})
    return days

# ---------- 全市場公司基本資料 ----------
def normalize_profile(rows, market):
    out={}
    for d in rows if isinstance(rows,list) else []:
        code=str(pick(d,["公司代號","SecuritiesCompanyCode","SecuritiesCode","證券代號","股票代號","Code"]) or "").strip()
        if not is_common_stock_code(code):continue
        name=str(pick(d,["公司簡稱","公司名稱","CompanyName","SecuritiesCompanyName","證券名稱","Name"]) or "").strip()
        industry=industry_zh(pick(d,["產業別","產業類別","Industry","IndustryName"]))
        out[code]={"code":code,"name":name or code,"market":market,"industry":industry}
    return out

def fetch_profiles():
    tw={};otc={}
    candidates=[
        ("TWSE","https://openapi.twse.com.tw/v1/opendata/t187ap03_L"),
        ("TPEx","https://openapi.twse.com.tw/v1/opendata/t187ap03_O"),
    ]
    for market,url in candidates:
        try:
            got=normalize_profile(get_json(url),market)
            if market=="TWSE":tw.update(got)
            else:otc.update(got)
        except Exception as e:print("profile",market,e)
    # TPEx fallback candidates
    if not otc:
        for url in [
            "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_company_basic_information",
            "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
        ]:
            try:
                otc.update(normalize_profile(get_json(url),"TPEx"))
                if otc:break
            except Exception as e:print("TPEx profile fallback",e)
    return tw,otc

# ---------- 全市場行情 ----------
def normalize_quotes(rows,market):
    out={}
    for d in rows if isinstance(rows,list) else []:
        code=str(pick(d,["Code","SecuritiesCompanyCode","SecuritiesCode","證券代號","股票代號"]) or "").strip()
        if not is_common_stock_code(code):continue
        close=num(pick(d,["ClosingPrice","Close","收盤價","收盤"]))
        pct=num(pick(d,["ChangePercent","漲跌幅","漲跌幅%"]))
        if pct is None:
            change=num(pick(d,["Change","漲跌價差"]))
            prev=None if close is None or change is None else close-change
            pct=None if prev in (None,0) else change/prev*100
        out[code]={"close":close,"ret1":None if pct is None else round(pct,2)}
    return out

def fetch_quotes():
    tw={};otc={}
    try:tw=normalize_quotes(get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"),"TWSE")
    except Exception as e:print("TWSE quotes",e)
    for url in ["https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes","https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"]:
        try:
            otc=normalize_quotes(get_json(url),"TPEx")
            if otc:break
        except Exception as e:print("TPEx quotes",e)
    return tw,otc

# ---------- 個股法人 ----------
def parse_twse_t86(d):
    ds=d.strftime("%Y%m%d")
    j=get_json(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={ds}&selectType=ALLBUT0999&response=json")
    fields=j.get("fields") or []; rows=j.get("data") or [];out={}
    def idx(tokens):
        for i,f in enumerate(fields):
            if any(t in str(f) for t in tokens):return i
        return None
    ic=idx(["證券代號"]); it=idx(["三大法人買賣超股數"])
    if it is None:
        cand=[i for i,f in enumerate(fields) if "買賣超股數" in str(f)]
        it=cand[-1] if cand else None
    for r in rows:
        try:
            code=str(r[ic if ic is not None else 0]).strip();sh=num(r[it]) if it is not None else None
            if is_common_stock_code(code) and sh is not None:out[code]=sh
        except:pass
    return out

def parse_tpex_insti():
    for url in ["https://www.tpex.org.tw/openapi/v1/tpex_mainboard_3insti_trading","https://www.tpex.org.tw/openapi/v1/tpex_3insti_trading"]:
        try:
            rows=get_json(url);out={}
            for d in rows if isinstance(rows,list) else []:
                code=str(pick(d,["SecuritiesCompanyCode","SecuritiesCode","證券代號","股票代號","Code"]) or "").strip()
                total=num(pick(d,["TotalNetBuySell","ThreeInstitutionalInvestorsNetBuySell","三大法人買賣超股數","合計買賣超股數","NetBuySell"]))
                if is_common_stock_code(code) and total is not None:out[code]=total
            if out:return out
        except Exception as e:print("TPEx insti",e)
    return {}

def shares_to_yi(shares,close):
    if shares is None or close is None:return None
    return round(shares*close/100_000_000,2)

# ---------- 每日全市場快照 / 動能 ----------
def load_snapshots():
    obj=load("stock_snapshots.json",{"days":[]})
    return obj.get("days",[])

def snapshot_history_value(days,code,back):
    vals=[]
    for d in days:
        x=(d.get("stocks") or {}).get(code)
        if x and x.get("close") is not None:vals.append((d["date"],x["close"]))
    if len(vals)<=back:return None
    a=vals[-back-1][1];b=vals[-1][1]
    return None if a in (None,0) else round((b/a-1)*100,2)

def score_stock(ret1,mom5,mom20,insti):
    parts=[
        50 if ret1 is None else 50+max(-15,min(15,ret1*4)),
        50 if mom5 is None else 50+max(-15,min(15,mom5*2)),
        50 if mom20 is None else 50+max(-12,min(12,mom20)),
        50 if insti is None else 50+max(-18,min(18,insti/2)),
    ]
    s=round(sum(parts)/len(parts))
    return s,("偏多" if s>=62 else "偏弱" if s<=38 else "觀望")

def build_full_market(market_days):
    if not market_days:return [],[]
    market_date=market_days[-1]["date"]
    twp,otp=fetch_profiles();twq,otq=fetch_quotes()
    try:twi=parse_twse_t86(datetime.fromisoformat(market_date).date())
    except Exception as e:print("T86 market date",market_date,e);twi={}
    oti=parse_tpex_insti()

    universe={**twp,**otp}
    # 如果 profile API 暫時失敗，行情仍能進 universe，但標成其他
    for code in twq:universe.setdefault(code,{"code":code,"name":code,"market":"TWSE","industry":"其他"})
    for code in otq:universe.setdefault(code,{"code":code,"name":code,"market":"TPEx","industry":"其他"})

    snaps=load_snapshots()
    # 清除舊版曾誤收 ETF/權證/非普通股代碼，減少手機載入量。
    for d in snaps:
        if isinstance(d.get("stocks"),dict):d["stocks"]={k:v for k,v in d["stocks"].items() if is_common_stock_code(str(k))}
    current_map={}
    for code,p in universe.items():
        q=(twq if p["market"]=="TWSE" else otq).get(code,{})
        close=q.get("close");ret1=q.get("ret1")
        sh=(twi if p["market"]=="TWSE" else oti).get(code)
        inst=shares_to_yi(sh,close)
        # 先把今日加入暫存，讓動能能使用既有日快照
        current_map[code]={"close":close,"ret1":ret1,"insti_yi":inst}

    # 同日期覆蓋，避免重複執行 Actions 造成重複日
    snaps=[d for d in snaps if d.get("date")!=market_date]
    snaps.append({"date":market_date,"stocks":current_map})
    snaps=sorted(snaps,key=lambda x:x.get("date",""))[-25:]
    save("stock_snapshots.json",{"days":snaps})

    stocks=[];groups={}
    for code,p in universe.items():
        q=current_map.get(code,{})
        mom5=snapshot_history_value(snaps,code,5)
        mom20=snapshot_history_value(snaps,code,20)
        score,signal=score_stock(q.get("ret1"),mom5,mom20,q.get("insti_yi"))
        x={**p,"close":q.get("close"),"ret1":q.get("ret1"),"mom5":mom5,"mom20":mom20,
           "insti_yi":q.get("insti_yi"),"score":score,"signal":signal}
        stocks.append(x)
        groups.setdefault(p["industry"],[]).append(x)

    # 板塊歷史用每日 sector net snapshot，讓 5/20 日逐日自然累積
    sector_hist=load("sector_history.json",{"days":[]}).get("days",[])
    today_net={}
    sectors=[]
    for name,members in groups.items():
        known=[x["insti_yi"] for x in members if x.get("insti_yi") is not None]
        daynet=round(sum(known),2) if known else None
        today_net[name]=daynet
    sector_hist=[d for d in sector_hist if d.get("date")!=market_date]
    sector_hist.append({"date":market_date,"net":today_net})
    sector_hist=sorted(sector_hist,key=lambda x:x.get("date",""))[-25:]
    save("sector_history.json",{"days":sector_hist})

    def hist_sum(name,n):
        vals=[d.get("net",{}).get(name) for d in sector_hist[-n:]]
        vals=[v for v in vals if v is not None]
        return round(sum(vals),2) if vals else None

    for name,members in groups.items():
        daynet=today_net.get(name);net5=hist_sum(name,5);net20=hist_sum(name,20)
        rets=[x["ret1"] for x in members if x.get("ret1") is not None]
        avgret=round(sum(rets)/len(rets),2) if rets else None
        ups=sum(1 for r in rets if r>0);downs=sum(1 for r in rets if r<0)
        state="資料建立中" if daynet is None else ("法人偏多" if daynet>10 else "法人偏空" if daynet<-10 else "資金分歧")
        sectors.append({
            "name":name,"state":state,"net_buy_value":daynet,
            "net_buy_text":"待資料" if daynet is None else f"{daynet:+.1f} 億",
            "net5_value":net5,"net5_text":"資料建立中" if net5 is None else f"{net5:+.1f} 億",
            "net20_value":net20,"net20_text":"資料建立中" if net20 is None else f"{net20:+.1f} 億",
            "ret5_value":None,"ret5_text":"逐日累積中",
            "ret1_value":avgret,"breadth":{"up":ups,"down":downs,"total":len(rets)},
            "note":f"官方產業分類｜{len(members)} 檔｜上漲 {ups} / 下跌 {downs}",
            "stocks":sorted(members,key=lambda x:(x.get("insti_yi") is not None,x.get("insti_yi") or -1e18),reverse=True),
            "related":[]
        })
    sectors.sort(key=lambda s:(s.get("net_buy_value") is not None,s.get("net_buy_value") or -1e18),reverse=True)
    stocks.sort(key=lambda x:(x.get("score") is not None,x.get("score") or 0),reverse=True)
    save("sectors.json",{"as_of":market_date,"classification":"官方產業別","sector_count":len(sectors),"sectors":sectors})
    return stocks,sectors

# ---------- 美國夜間消息 / 盤前修正 ----------
def get_text(url,timeout=30):
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 tw-stock-ai/1.0","Accept":"text/html,application/rss+xml,text/plain,*/*"})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return r.read().decode("utf-8",errors="replace")

def clean_title(s):
    s=unescape(re.sub(r"<[^>]+>","",str(s or ""))).strip()
    return re.sub(r"\s+"," ",s)

def headline_impact(title):
    t=title.lower()
    positive=["降息","寬鬆","上漲","大漲","創高","超預期","利多","強勁","反彈","growth","rally","rate cut","beats"]
    negative=["升息","關稅","制裁","戰爭","衝突","暴跌","大跌","衰退","通膨升溫","利空","下修","selloff","tariff","war","recession","misses"]
    score=sum(1 for k in positive if k in t)-sum(1 for k in negative if k in t)
    return max(-2,min(2,score))

def fetch_google_news():
    q='美股 OR 那斯達克 OR 費城半導體 OR 聯準會 OR Fed OR NVIDIA OR 台積電 ADR OR 關稅 OR AI 伺服器'
    url='https://news.google.com/rss/search?q='+urllib.parse.quote(q)+'&hl=zh-TW&gl=TW&ceid=TW:zh-Hant'
    xml=get_text(url)
    root=ET.fromstring(xml)
    items=[]
    for it in root.findall('.//item')[:18]:
        title=clean_title(it.findtext('title'))
        link=(it.findtext('link') or '').strip()
        pub=(it.findtext('pubDate') or '').strip()
        source=''
        se=it.find('source')
        if se is not None and se.text:source=clean_title(se.text)
        if title:items.append({"title":title,"link":link,"published":pub,"source":source,"impact":headline_impact(title)})
    return items

def fetch_stooq(symbol,label):
    # 免費公開 CSV；抓最近幾日以估算美股/期貨方向。失敗時保留 null，不捏造。
    end=NOW.strftime('%Y%m%d'); start=(NOW-timedelta(days=10)).strftime('%Y%m%d')
    url=f'https://stooq.com/q/d/l/?s={urllib.parse.quote(symbol)}&d1={start}&d2={end}&i=d'
    rows=list(csv.DictReader(io.StringIO(get_text(url))))
    vals=[]
    for r in rows:
        c=num(r.get('Close'))
        if c is not None:vals.append((r.get('Date'),c))
    if not vals:return {"label":label,"symbol":symbol,"close":None,"change_pct":None}
    last=vals[-1]; prev=vals[-2][1] if len(vals)>=2 else None
    pct=None if prev in (None,0) else round((last[1]/prev-1)*100,2)
    return {"label":label,"symbol":symbol,"date":last[0],"close":last[1],"change_pct":pct}

def update_overnight():
    old=load('overnight.json',{})
    try:headlines=fetch_google_news()
    except Exception as e:
        print('overnight news',e);headlines=old.get('headlines',[])
    markets=[]
    for sym,label in [('^spx','S&P 500'),('^ndq','NASDAQ'),('^dji','道瓊')]:
        try:markets.append(fetch_stooq(sym,label))
        except Exception as e:
            print('overnight market',sym,e);markets.append({"label":label,"symbol":sym,"close":None,"change_pct":None})
    news_score=sum(x.get('impact',0) for x in headlines[:10])
    mvals=[x.get('change_pct') for x in markets if x.get('change_pct') is not None]
    market_score=0 if not mvals else sum(mvals)/len(mvals)
    raw=market_score*1.5 + news_score*0.35
    bias='偏多' if raw>0.8 else '偏空' if raw<-0.8 else '中性'
    confidence=round(min(75,max(50,50+abs(raw)*6)))
    obj={
      "updated_at":NOW.isoformat(timespec='seconds'),
      "bias":bias,"confidence":confidence,"score":round(raw,2),
      "markets":markets,"headlines":headlines[:12],
      "note":"這是盤後正式預測之外的夜間即時修正，只使用公開美股行情與最新新聞；不會覆寫已事前登記的正式預測。"
    }
    save('overnight.json',obj)
    return obj

# ---------- 預測 / 驗證 ----------
def default_model():
    return {"generation":0,"version":"baseline-1.0","weights":{"mom5":0.55,"flow5":0.45},"min_verified_for_tuning":20,"last_tuned_at":None}

def model_score(f,w):
    return float(w.get("mom5",.55))*float(f.get("mom5",0) or 0)+float(w.get("flow5",.45))*(float(f.get("flow5",0) or 0)/300)

def direction(score):return "偏多" if score>.35 else "偏空" if score<-.35 else "震盪"

def tune(preds,model):
    verified=[p for p in preds if p.get("verified") and p.get("inputs_snapshot")]
    if len(verified)<model.get("min_verified_for_tuning",20):return model
    cur=model.get("weights",{"mom5":.55,"flow5":.45})
    def acc(w):
        return sum(direction(model_score(p["inputs_snapshot"],w))==p.get("actual_direction") for p in verified)/len(verified)
    old=acc(cur);best=(old,cur)
    for i in range(2,9):
        w={"mom5":i/10,"flow5":1-i/10};a=acc(w)
        if a>best[0]:best=(a,w)
    if best[0]>old+.03 and best[1]!=cur:
        model["generation"]=int(model.get("generation",0))+1;model["weights"]=best[1]
        model["version"]=f"adaptive-1.0-g{model['generation']}";model["last_tuned_at"]=NOW.isoformat(timespec="seconds")
    return model

def make_forecast(days,model):
    usable=[x for x in days if x.get("return") is not None and x.get("total") is not None]
    if len(usable)<20:return None
    last=usable[-1]
    f={"mom5":round(sum(float(x.get("return") or 0) for x in usable[-5:]),2),
       "flow5":round(sum(float(x.get("total") or 0) for x in usable[-5:]),2)}
    sc=model_score(f,model.get("weights",{}));d=direction(sc)
    rets=[x["return"] for x in usable[-20:]];mean=sum(rets)/len(rets)
    vol=math.sqrt(sum((r-mean)**2 for r in rets)/len(rets))
    return {"prediction_for":next_weekday(datetime.fromisoformat(last["date"]).date()).isoformat(),
      "made_at":NOW.isoformat(timespec="seconds"),"direction":d,"confidence":round(min(80,max(52,52+abs(sc)*6))),
      "range":f"約 ±{vol:.2f}%","reason":f"近5日指數動能 {f['mom5']:+.2f}%，三大法人合計 {f['flow5']:+.2f} 億。",
      "model_version":model.get("version","baseline-1.0"),"model_generation":model.get("generation",0),"inputs_snapshot":f}

def verify(days,preds):
    by={x["date"]:x for x in days}
    for p in preds:
        if p.get("verified"):continue
        real=by.get(p.get("prediction_for"))
        if not real or real.get("return") is None:continue
        rr=real["return"];a="偏多" if rr>.2 else "偏空" if rr<-.2 else "震盪"
        p.update({"verified":True,"actual_return":rr,"actual_direction":a,"correct":a==p.get("direction")})
        if p["correct"]:p["error_reason"]="方向命中。"
        else:
            then=(p.get("inputs_snapshot") or {}).get("flow5");now=sum(x.get("total") or 0 for x in days[-5:]);why=[]
            if then is not None and then*now<0:why.append("近5日法人資金方向反轉")
            if abs(rr)>=2:why.append("實際單日波動明顯放大")
            if not why:why.append("市場未延續預測當下的短期動能與資金訊號")
            p["error_reason"]="；".join(why)+"。"
    return preds

def full_update():
    overnight=update_overnight()
    days=market_backfill()
    stocks,sectors=build_full_market(days)
    pobj=load("predictions.json",{"predictions":[]})
    if isinstance(pobj,list):pobj={"predictions":pobj}
    preds=verify(days,pobj.get("predictions",[]))
    model=load("model.json",default_model())
    if "weights" not in model or "flow5" not in model.get("weights",{}):model=default_model()
    if str(model.get("version","")).startswith("baseline-0."):model["version"]="baseline-1.0"
    model=tune(preds,model)
    fc=make_forecast(days,model)
    if fc and not any(p.get("prediction_for")==fc["prediction_for"] for p in preds):preds.append(fc)
    save("predictions.json",{"predictions":preds[-300:]});save("model.json",model)

    latest=load("latest.json",{})
    if days:
        latest["market_date"]=days[-1]["date"];latest["market_return"]=days[-1].get("return");latest["history_days"]=len(days)
    # latest 只放排行前 120 檔，完整成分股放 sectors.json，避免手機每次載入超大 JSON。
    sector_summary=[{k:v for k,v in x.items() if k!="stocks"} for x in sectors]
    latest.update({"updated_at":NOW.isoformat(timespec="seconds"),"engine":ENGINE,"forecast":fc,"model":model,
                   "stocks":stocks[:120],"sectors":sector_summary,"overnight":{k:v for k,v in overnight.items() if k!="headlines"},
                   "summary":f"全市場官方產業板塊已自動建立，共 {len(sectors)} 類、{len(stocks)} 檔普通股。夜間美股與新聞另外每小時更新，不改寫正式事前預測。"})
    verified=[p for p in preds if p.get("verified")]
    latest["verification"]={"html":("<b>尚無可驗證的正式預測。</b><br>事前預測已保存，交易日收盤後會自動核對。" if not verified else
        f"<b>{'命中' if verified[-1]['correct'] else '未命中'}</b><br>預測 {verified[-1]['direction']}，實際 {verified[-1]['actual_direction']}（{verified[-1]['actual_return']:+.2f}%）。{verified[-1]['error_reason']}")}
    save("latest.json",latest)
    save("backfill_meta.json",{"updated_at":NOW.isoformat(timespec="seconds"),"market_days":len(days),"target_market_days":60,
      "stock_detail_count":len(stocks),"sector_count":len(sectors),"classification":"TWSE/TPEx 官方產業別（中文）","engine":ENGINE})
    print(ENGINE,"FULL market",len(days),"stocks",len(stocks),"sectors",len(sectors))

def live_update():
    obj=update_overnight()
    print(ENGINE,"LIVE overnight",obj.get('bias'),obj.get('confidence'))

def main():
    # 手動 Run workflow 永遠做完整更新；排程 18 點台灣時間做收盤完整更新，其他夜間排程只更新美股/新聞。
    event=os.getenv('GITHUB_EVENT_NAME','')
    mode=os.getenv('TW_STOCK_MODE','').lower()
    if mode=='full' or event=='workflow_dispatch' or NOW.hour==18:
        full_update()
    else:
        live_update()

if __name__=="__main__":main()
