"""Simple local performance notes used by the Viral Analyzer feedback loop."""
from __future__ import annotations
import json, datetime
from pathlib import Path
from chopster.app.paths import user_data_dir
PATH=user_data_dir()/"clip_analytics.json"

def load():
    try:return json.loads(PATH.read_text(encoding="utf-8")) if PATH.exists() else []
    except Exception:return []

def record(clip_id, platform, views=0, likes=0, comments=0, shares=0, retention=0.0):
    rows=load(); rows.append({"clip_id":clip_id,"platform":platform,"views":int(views),"likes":int(likes),"comments":int(comments),"shares":int(shares),"retention":float(retention),"ts":datetime.datetime.now().isoformat(timespec="seconds")}); PATH.parent.mkdir(parents=True,exist_ok=True); PATH.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf-8"); return rows[-1]

def summary():
    rows=load(); views=sum(int(x.get("views",0)) for x in rows); likes=sum(int(x.get("likes",0)) for x in rows); comments=sum(int(x.get("comments",0)) for x in rows); shares=sum(int(x.get("shares",0)) for x in rows)
    engagement=(likes+comments+shares)/views*100 if views else 0.0
    return {"clips":len(rows),"views":views,"likes":likes,"comments":comments,"shares":shares,"engagement_rate":engagement}

