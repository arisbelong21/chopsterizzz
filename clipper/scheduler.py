"""Local publishing/scheduling queue. It stores jobs safely without pretending to publish when no platform API is configured."""
from __future__ import annotations
import json, uuid, datetime
from pathlib import Path
from chopster.app.paths import user_data_dir

PATH=user_data_dir()/"publish_queue.json"

def _load():
    try:return json.loads(PATH.read_text(encoding="utf-8")) if PATH.exists() else []
    except Exception:return []

def _save(items):
    PATH.parent.mkdir(parents=True,exist_ok=True); PATH.write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding="utf-8")

def add_job(file_path,platform,title="",description="",hashtags=None,scheduled_at=""):
    items=_load(); job={"id":uuid.uuid4().hex[:10],"file":str(file_path),"platform":platform,"title":title,"description":description,"hashtags":hashtags or [],"scheduled_at":scheduled_at,"status":"scheduled" if scheduled_at else "ready","created_at":datetime.datetime.now().isoformat(timespec="seconds")}; items.append(job); _save(items); return job

def list_jobs(): return _load()

def remove_job(job_id):
    items=[x for x in _load() if x.get("id")!=job_id]; _save(items)
