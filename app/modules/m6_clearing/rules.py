from decimal import Decimal
from app.reasons import sources
from app.errors import DomainError
def evaluate(rows,context):
    intake=context.get("intake")
    bad=context.get("quarantine",[])
    accepted=set(x for row in rows for x in sources(row))
    rejected=set(x for row in bad for x in sources(row))
    if accepted & rejected: raise DomainError("LINEAGE_OVERLAP","同一来源行被重复裁决")
    m={"accepted_inputs":len(accepted),"quarantine_inputs":len(rejected),"output_records":len(rows)}
    if intake:
        excluded=intake["duplicate"]+intake["blank"]+intake["repeated_headers"]
        m.update(input=intake["input"],excluded=excluded,balanced=intake["input"]==len(accepted)+len(rejected)+excluded)
    else:m.update(input=len(accepted)+len(rejected),excluded=0,balanced=True)
    output=[]
    for row in rows:
        expected=Decimal(str(row.get("expected","0")));reported=Decimal(str(row.get("reported","0")))
        output.append({**row,"difference":str(expected-reported),"status":"通过" if expected==reported else "差异"})
    shuttle=[r for r in output if r.get("shuttle_status") not in (None,"—")]
    m.update(expected_total=str(sum((Decimal(str(r.get("expected","0"))) for r in rows),Decimal(0))),reported_total=str(sum((Decimal(str(r.get("reported","0"))) for r in rows),Decimal(0))),differences=sum(r["status"]=="差异" for r in output))
    m.update(shuttle_merged=sum(r.get("shuttle_status")=="并入续程" for r in output),shuttle_charged=sum(r.get("shuttle_status")=="仍需收费" for r in output),shuttle_unverified=sum(r.get("shuttle_status")=="未核清" for r in output),shuttle_exempted_total=str(sum((Decimal(str(r.get("shuttle_exemption","0"))) for r in output),Decimal(0))),shuttle_receivable_difference=str(sum((Decimal(r["difference"]) for r in shuttle),Decimal(0))))
    return {"rows":output,"quarantine":[],"metrics":m}
