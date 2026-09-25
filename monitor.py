import json,re,sys
from pathlib import Path
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

MIN_DISCOUNT=30
STATE=Path("state.json")
REPORT=Path("last_check.json")
URLS=[
"https://bill.hostdare.com/announcements",
"https://bill.hostdare.com/store/premium-china-optimized-nvme-kvm",
"https://bill.hostdare.com/store/premium-china-optimized-amd-kvm-vps-usa",
"https://bill.hostdare.com/store/premium-china-optimized-kvm-vps",
]
HEADERS={"User-Agent":"Mozilla/5.0","Accept-Language":"en-US,en;q=0.9"}

def get_text(url):
    r=requests.get(url,headers=HEADERS,timeout=25,allow_redirects=True)
    s=BeautifulSoup(r.text,"html.parser")
    return r.status_code,r.url,(s.title.get_text(" ",strip=True) if s.title else ""),re.sub(r"\s+"," ",s.get_text(" ",strip=True))

def load():
    if not STATE.exists(): return {"keys":[]}
    try:
        d=json.loads(STATE.read_text(encoding="utf-8"))
        return d if isinstance(d,dict) else {"keys":[]}
    except: return {"keys":[]}

def main():
    hits=[]; pages=[]
    for url in URLS:
        try:
            status,final,title,text=get_text(url)
            ph=[]
            low=text.lower()
            if status==200 and "cn2" in low:
                for m in re.finditer(r"(\d{1,2})\s*%\s*(?:off|discount)?",text,re.I):
                    pct=int(m.group(1))
                    if pct<MIN_DISCOUNT: continue
                    seg=text[max(0,m.start()-250):min(len(text),m.end()+350)]
                    sl=seg.lower()
                    if ("cn2" in sl or "china optimized" in sl) and any(x in sl for x in ("recurring","coupon","code","promo","offer","discount")):
                        cm=re.search(r"(?:code|coupon)\s*[:：]?\s*([A-Z0-9]{6,20})",seg,re.I)
                        code=cm.group(1).upper() if cm else ""
                        key=f"discount:{pct}:{code or title[:50]}"
                        ph.append({"kind":"discount","key":key,"url":final,"detail":f"{pct}%"+(f" / code {code}" if code else "")})
                for product in ("CSSD1","CAMD1"):
                    mm=re.search(rf"\b{product}\b",text,re.I)
                    if mm:
                        seg=text[mm.start():mm.start()+700]
                        am=re.search(r"\b(\d+)\s+(?:Available|可用)\b",seg,re.I)
                        if am and int(am.group(1))>0 and "cn2 gia" in seg.lower():
                            ph.append({"kind":"stock","key":f"stock:{product}","url":final,"detail":f"{product}: {am.group(1)} Available"})
            hits.extend(ph)
            pages.append({"url":url,"status":status,"final_url":final,"title":title,"hits":ph})
        except Exception as e:
            pages.append({"url":url,"status":None,"error":f"{type(e).__name__}: {e}"})

    uniq={h["key"]:h for h in hits}
    hits=list(uniq.values())
    old=set(load().get("keys",[]))
    current=set(uniq)
    new=[h for h in hits if h["key"] not in old]

    STATE.write_text(json.dumps({"keys":sorted(current),"updated_at":datetime.now(timezone.utc).isoformat()},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    report={"checked_at":datetime.now(timezone.utc).isoformat(),"min_discount":MIN_DISCOUNT,"new_hits":new,"current_hits":hits,"pages":pages}
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if new:
        print("\n🚨 HostDare CN2 GIA 新活动/补货：")
        for h in new: print("-",h["detail"],h["url"])
        return 42
    return 0

if __name__=="__main__":
    raise SystemExit(main())
