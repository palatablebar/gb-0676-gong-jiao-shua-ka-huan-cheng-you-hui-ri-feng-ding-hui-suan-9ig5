import csv,io,json
import pytest
from decimal import Decimal
from app.pipeline import calculate,execute,load_sample
from app.modules.m4_journeys.rules import evaluate as pair
from app.modules.m5_discounts.rules import evaluate as discount
from app.modules.m6_clearing.rules import evaluate as reconcile
from app.modules.m7_settlement.rules import evaluate as seal
from app.errors import DomainError
from app import store
ORDERS=[{"id":"F001","route":"01","shuttle_route":"02","start":"2026-11-16T07:30:00+08:00","end":"2026-11-16T09:30:00+08:00"}]
def tap(eid,card="0001",route="01",at="2026-11-16T08:00:00+08:00",note="常规",reported="2.50"):
    return {"event_id":eid,"card":card,"route":route,"at":at,"event_time":at,"result":"success","reported":reported,"note":note,"base_fare":"2.50","_line":1,"_sources":[1]}
def ctx(orders=ORDERS): return {"parameters":{"fault_orders":orders}}
def paired(rows,orders=ORDERS): return pair(rows,ctx(orders))
class TestDetermination:
    def test_merged_inherits_original_journey(self):
        r=paired([tap("A1"),tap("A2",route="02",at="2026-11-16T08:20:00+08:00",note="故障单:F001")])
        first,second=r["rows"]
        assert second["shuttle_status"]=="并入续程" and second["journey"]==first["journey"]
        assert second["continues_from"]=="A1" and first["continued_by"]=="A2" and second["voucher"]=="F001"
        assert r["metrics"]["shuttle_merged"]==1
    def test_borrowed_voucher_still_charged(self):
        "TRAP-28 普通车次借用故障凭证不得免扣"
        r=paired([tap("A1"),tap("A2",route="03",at="2026-11-16T08:20:00+08:00",note="故障单:F001")])
        assert r["rows"][1]["shuttle_status"]=="仍需收费" and r["metrics"]["shuttle_charged"]==1
    def test_retap_outside_window_still_charged(self):
        r=paired([tap("A1"),tap("A2",route="02",at="2026-11-16T10:00:00+08:00",note="故障单:F001")])
        assert r["rows"][1]["shuttle_status"]=="仍需收费"
    def test_unknown_voucher_unverified(self):
        r=paired([tap("A1"),tap("A2",route="02",at="2026-11-16T08:20:00+08:00",note="故障单:F999")])
        assert r["rows"][1]["shuttle_status"]=="未核清" and r["metrics"]["shuttle_unverified"]==1
    def test_empty_registry_unverified(self):
        r=pair([tap("A1",note="故障单:F001")],ctx([]))
        assert r["rows"][0]["shuttle_status"]=="未核清"
    def test_incomplete_order_unverified(self):
        bad=[{"id":"F001","route":"01","shuttle_route":"02","start":"2026-11-16T09:30:00+08:00","end":"2026-11-16T07:30:00+08:00"}]
        r=pair([tap("A1"),tap("A2",route="02",at="2026-11-16T08:20:00+08:00",note="故障单:F001")],ctx(bad))
        assert r["rows"][1]["shuttle_status"]=="未核清"
    def test_duplicate_order_id_rejected(self):
        with pytest.raises(DomainError) as e: pair([],ctx(ORDERS+ORDERS))
        assert e.value.code=="FAULT_ORDER_CONFLICT"
    def test_missing_original_unverified(self):
        r=paired([tap("A2",route="02",at="2026-11-16T08:20:00+08:00",note="故障单:F001")])
        assert r["rows"][0]["shuttle_status"]=="未核清" and "原始刷卡" in r["rows"][0]["shuttle_detail"]
    def test_retry_not_double_exempted(self):
        "TRAP-29 同一补刷重试只认定一次"
        r=paired([tap("A1"),tap("A2",route="02",at="2026-11-16T08:20:00+08:00",note="故障单:F001"),tap("A3",route="02",at="2026-11-16T08:21:00+08:00",note="故障单:F001 重试")])
        assert sum(x["shuttle_status"]=="并入续程" for x in r["rows"])==1
        assert r["quarantine"][0]["reason"]=="SHUTTLE_RETRY" and r["quarantine"][0]["event_id"]=="A3"
        assert r["metrics"]["shuttle_retries"]==1
    def test_same_voucher_other_card_independent(self):
        rows=[tap("A1"),tap("A2",route="02",at="2026-11-16T08:20:00+08:00",note="故障单:F001"),tap("B1",card="0002",at="2026-11-16T08:05:00+08:00"),tap("B2",card="0002",route="02",at="2026-11-16T08:25:00+08:00",note="故障单:F001")]
        r=paired(rows);assert r["metrics"]["shuttle_merged"]==2 and r["metrics"]["shuttle_retries"]==0
    def test_chained_fault_inherits_same_journey(self):
        orders=ORDERS+[{"id":"F002","route":"02","shuttle_route":"03","start":"2026-11-16T08:00:00+08:00","end":"2026-11-16T10:00:00+08:00"}]
        rows=[tap("A1"),tap("A2",route="02",at="2026-11-16T08:20:00+08:00",note="故障单:F001"),tap("A3",route="03",at="2026-11-16T08:40:00+08:00",note="故障单:F002")]
        r=pair(rows,ctx(orders))
        assert all(x["journey"]==r["rows"][0]["journey"] for x in r["rows"]) and r["metrics"]["shuttle_merged"]==2
    def test_plain_tap_untouched(self):
        r=paired([tap("A1")]);assert r["rows"][0]["shuttle_status"]=="—" and r["rows"][0]["voucher"]==""
def ride(eid,minute,route="01",status="—",base="2.50"):
    return {"event_id":eid,"card":"0001","business_day":"2026-11-16","at":f"2026-11-16T{8+minute//60:02d}:{minute%60:02d}:00+08:00","route":route,"base_fare":base,"shuttle_status":status}
class TestDiscount:
    def test_merged_exempt_and_conserved(self):
        "TRAP-27 接驳免扣进入分币守恒"
        r=discount([ride("A1",0),ride("A2",20,"02","并入续程")],{})["rows"]
        m=r[1];assert m["expected"]=="0.00" and m["shuttle_exemption"]=="2.50"
        assert all(Decimal(x["base_fare"])==Decimal(x["expected"])+Decimal(x["transfer_discount"])+Decimal(x["cap_discount"])+Decimal(x["shuttle_exemption"]) for x in r)
    def test_merged_not_anchor(self):
        "补刷不延长换乘锚点，连续关系锚在原车次"
        r=discount([ride("A1",0),ride("A2",20,"02","并入续程"),ride("A3",75,"03")],{})["rows"]
        assert r[2]["transfer_discount"]=="0.00" and r[2]["expected"]=="2.50"
    def test_continuity_window_from_original(self):
        r=discount([ride("A1",0),ride("A2",20,"02","并入续程"),ride("A3",50,"03")],{})["rows"]
        assert r[2]["transfer_anchor"]=="A1" and r[2]["expected"]=="1.50"
    def test_merged_not_consume_cap(self):
        r=discount([ride("A1",0),ride("A2",20,"02","并入续程"),ride("A3",50,"03")],{"parameters":{"daily_cap":"3.00"}})["rows"]
        assert r[2]["expected"]=="0.50" and r[2]["day_paid"]=="3.00"
    def test_charged_retap_normal_rules(self):
        r=discount([ride("A1",0),ride("A2",20,"03","仍需收费")],{})["rows"]
        assert r[1]["expected"]=="1.50" and r[1]["shuttle_exemption"]=="0.00"
    def test_metrics(self):
        m=discount([ride("A1",0),ride("A2",20,"02","并入续程")],{})["metrics"]
        assert m["shuttle_merged"]==1 and m["shuttle_exempted_total"]=="2.50"
class TestClearingAndSeal:
    def test_receivable_difference(self):
        rows=[{**ride("A1",0),"expected":"2.50","reported":"2.50","shuttle_exemption":"0.00"},{**ride("A2",20,"02","并入续程"),"expected":"0.00","reported":"2.50","shuttle_exemption":"2.50"}]
        m=reconcile(rows,{})["metrics"]
        assert m["shuttle_merged"]==1 and m["shuttle_receivable_difference"]=="-2.50" and m["shuttle_exempted_total"]=="2.50"
    def test_unverified_blocks_seal(self):
        "TRAP-30 归属未核清不得封存日结"
        m=seal([{"event_id":"A1","shuttle_status":"未核清"}],{})["metrics"]
        assert m["ready"] is False and m["sealed"] is False and m["sha256"] is None and m["unverified_shuttles"]==1
    def test_verified_seals(self):
        m=seal([{"event_id":"A1","shuttle_status":"并入续程"}],{})["metrics"]
        assert m["ready"] is True and m["sealed"] is True and len(m["sha256"])==64
    def test_charged_retap_does_not_block_seal(self):
        assert seal([{"event_id":"A1","shuttle_status":"仍需收费"}],{})["metrics"]["sealed"] is True
SEALABLE="""event_id,card,route,event_time,result,reported,note
E101,0001,01,2026-11-16T08:00:00+08:00,success,2.50,常规
E102,0001,02,2026-11-16T08:20:00+08:00,success,2.50,故障单:F001 接驳补刷
E103,0001,03,2026-11-16T08:50:00+08:00,success,2.50,常规
E501,0005,01,2026-11-16T08:00:00+08:00,success,2.50,常规
E502,0005,02,2026-11-16T08:20:00+08:00,success,0,故障单:F001 接驳补刷
E503,0005,02,2026-11-16T08:21:00+08:00,success,0,故障单:F001 补刷重试
"""
class TestEndToEnd:
    def test_fault_sample_conclusion(self):
        r=calculate(load_sample("fault.csv"))
        m4,m5,m6,m7=(r["stages"][i]["metrics"] for i in (3,4,5,6))
        assert (m4["shuttle_merged"],m4["shuttle_charged"],m4["shuttle_unverified"],m4["shuttle_retries"])==(3,1,1,1)
        assert m5["expected_total"]=="24.50" and m5["shuttle_exempted_total"]=="7.50"
        assert m6["balanced"] is True and m6["shuttle_receivable_difference"]=="-2.50"
        assert m7["ready"] is False and m7["sha256"] is None
    def test_fault_sample_journey_merged(self):
        rows=calculate(load_sample("fault.csv"))["stages"][3]["rows"]
        by_id={r["event_id"]:r for r in rows}
        assert by_id["E102"]["journey"]==by_id["E101"]["journey"] and by_id["E102"]["continues_from"]=="E101"
        assert by_id["E302"]["shuttle_status"]=="仍需收费" and by_id["E401"]["shuttle_status"]=="未核清"
    def test_unsealed_archive_409(self,client):
        p=client.post("/api/runs",json={"sample":"fault.csv"}).get_json()
        r=client.get("/api/runs/"+p["run_id"]+"/archive")
        assert r.status_code==409 and r.get_json()["code"]=="UNSEALED"
    def test_unsealed_batch_still_reviewable(self,client):
        p=client.post("/api/runs",json={"sample":"fault.csv"}).get_json()
        assert client.post("/api/runs/"+p["run_id"]+"/review",json={"version":1,"decision":"已复核"}).status_code==200
        assert client.get("/journeys").status_code==200 and "并入续程" in client.get("/journeys").data.decode()
    def test_sealable_fault_run_exports_records(self,client):
        p=client.post("/api/runs",json={"csv":SEALABLE}).get_json()
        assert p["stages"][6]["metrics"]["ready"] is True
        r=client.get("/api/runs/"+p["run_id"]+"/archive");assert r.status_code==200
        rows=list(csv.reader(io.StringIO(r.data.decode("utf-8-sig"))))
        accepted=[x for x in rows[1:] if x[3]=="accepted"];quarantine=[x for x in rows[1:] if x[3]=="quarantine"]
        evidence="".join(x[6] for x in accepted)
        assert "F001" in evidence and "E101" in evidence and "故障单" in evidence
        assert any(x[5]=="SHUTTLE_RETRY" and "E503" in x[6] for x in quarantine)
    def test_fault_rerun_idempotent(self,client):
        a=client.post("/api/runs",json={"sample":"fault.csv"}).get_json()
        b=client.post("/api/runs",json={"sample":"fault.csv"}).get_json()
        assert a["run_id"]==b["run_id"] and len(store.history())==1
    def test_parameters_override_registry(self,client):
        orders=[{"id":"F001","route":"01","shuttle_route":"02","start":"2026-11-16T07:30:00+08:00","end":"2026-11-16T09:30:00+08:00"},{"id":"F999","route":"01","shuttle_route":"02","start":"2026-11-16T07:30:00+08:00","end":"2026-11-16T09:30:00+08:00"}]
        p=client.post("/api/runs",json={"sample":"fault.csv","parameters":{"fault_orders":orders}}).get_json()
        rows={r["event_id"]:r for r in p["stages"][3]["rows"]}
        assert rows["E401"]["shuttle_status"]=="未核清" and "原始刷卡" in rows["E401"]["shuttle_detail"]
        assert rows["E102"]["shuttle_status"]=="并入续程"
    def test_bad_fault_orders_rejected(self,client):
        r=client.post("/api/runs",json={"sample":"clean.csv","parameters":{"fault_orders":"F001"}})
        assert r.status_code==422 and r.get_json()["code"]=="BAD_FAULT_ORDERS"
