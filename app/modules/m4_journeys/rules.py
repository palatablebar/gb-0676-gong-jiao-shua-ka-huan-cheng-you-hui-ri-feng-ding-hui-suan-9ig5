import re
from app.reasons import reject
from app.values import instant
from app.errors import DomainError
from app.modules.m2_routes.rules import ROUTES
from datetime import timezone,timedelta
VOUCHER=re.compile(r"故障单\s*[:：]\s*([0-9A-Za-z_-]+)")
DEFAULT_FAULT_ORDERS=[{"id":"F001","route":"01","shuttle_route":"02","start":"2026-11-16T07:30:00+08:00","end":"2026-11-16T09:30:00+08:00"}]
def registry(context):
    params=context.get("parameters",{}) if isinstance(context,dict) else {}
    raw=params.get("fault_orders",DEFAULT_FAULT_ORDERS)
    if not isinstance(raw,list): raise DomainError("BAD_FAULT_ORDERS","故障单登记需要列表")
    orders={}
    for entry in raw:
        if not isinstance(entry,dict): raise DomainError("BAD_FAULT_ORDER","故障单需要对象")
        oid=str(entry.get("id","")).strip()
        if not oid: raise DomainError("BAD_FAULT_ORDER","故障单缺少编号")
        if oid in orders: raise DomainError("FAULT_ORDER_CONFLICT","故障单编号重复")
        order={"id":oid,"valid":False}
        try:
            route=ROUTES.get(str(entry.get("route","")).strip());shuttle=ROUTES.get(str(entry.get("shuttle_route","")).strip())
            start=instant(entry["start"]);end=instant(entry["end"])
            if route and shuttle and start<end: order.update(route=route,shuttle_route=shuttle,start=start,end=end,valid=True)
        except (KeyError,TypeError,ValueError,DomainError): pass
        orders[oid]=order
    return orders
def evaluate(rows,context):
    orders=registry(context)
    good=[];bad=[];seen=set();determined={};by_card={}
    merged=charged=unverified=retries=0
    def accept(row):
        good.append(row);by_card.setdefault(row["card"],[]).append(row)
    for row in sorted(rows,key=lambda r:(r.get("card",""),r["at"],r.get("event_id",""))):
        if row.get("result")!="success":bad.append(reject(row,"FAILED_TAP","失败刷卡不构成行程"));continue
        key=(row["card"],row["at"])
        if key in seen:bad.append(reject(row,"SIMULTANEOUS_TAP","同卡同刻多行程"));continue
        seen.add(key);at=instant(row["at"])
        base={**row,"business_day":at.astimezone(timezone(timedelta(hours=8))).date().isoformat(),"journey":row["card"]+"-"+row["event_id"],"shuttle_status":"—","voucher":"","shuttle_detail":"","continues_from":"—","continued_by":"—"}
        match=VOUCHER.search(row.get("note","") or "")
        if not match: accept(base);continue
        voucher=match.group(1);base["voucher"]=voucher
        dkey=(row["card"],voucher)
        if dkey in determined:
            retries+=1;bad.append(reject(base,"SHUTTLE_RETRY","同一补刷重试，并入首次认定 "+determined[dkey]+"，不重复免扣"));continue
        determined[dkey]=row["event_id"]
        order=orders.get(voucher)
        if order is None:
            unverified+=1;accept({**base,"shuttle_status":"未核清","shuttle_detail":"故障单未登记，归属待核，暂按普通行程收费"});continue
        if not order["valid"]:
            unverified+=1;accept({**base,"shuttle_status":"未核清","shuttle_detail":"故障单信息不全，归属待核，暂按普通行程收费"});continue
        candidates=[r for r in by_card.get(row["card"],[]) if r["route"]==order["route"] and r.get("shuttle_status") in ("—","并入续程") and order["start"]<=instant(r["at"])<order["end"] and instant(r["at"])<=at]
        if not candidates:
            unverified+=1;accept({**base,"shuttle_status":"未核清","shuttle_detail":"未找到故障车次原始刷卡，归属待核，暂按普通行程收费"});continue
        original=max(candidates,key=lambda r:(instant(r["at"]),r["event_id"]))
        if row["route"]!=order["shuttle_route"]:
            charged+=1;accept({**base,"shuttle_status":"仍需收费","shuttle_detail":"补刷线路非故障单接驳线路，疑似借用凭证，按普通行程收费"});continue
        if not order["start"]<=at<order["end"]:
            charged+=1;accept({**base,"shuttle_status":"仍需收费","shuttle_detail":"补刷不在故障时段内，按普通行程收费"});continue
        merged+=1;original["continued_by"]=row["event_id"]
        accept({**base,"journey":original["journey"],"shuttle_status":"并入续程","continues_from":original["event_id"],"shuttle_detail":"故障单"+voucher+"接驳车次，继承原车次连续关系，并入原行程"})
    return {"rows":good,"quarantine":bad,"metrics":{"journeys":len(good),"cards":len(set(r["card"] for r in good)),"shuttle_merged":merged,"shuttle_charged":charged,"shuttle_unverified":unverified,"shuttle_retries":retries}}
