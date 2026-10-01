import uvicorn
import os
import json
import uuid
import boto3
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
DATA_FILE = os.environ.get("DATA_FILE", "/mnt/efs/urls.json")
CLUSTER_NAME = os.environ.get("CLUSTER_NAME")
MONITOR_TASK_DEF_ARN = os.environ.get("MONITOR_TASK_DEF_ARN")

app = FastAPI(title="URL Shortener", version="1.0.0")

class UrlRequest(BaseModel):
    url: str

def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.post("/shorten")
def shorten_url(req: UrlRequest):
    data = load_data()
    short_id = str(uuid.uuid4())[:8]
    data[short_id] = req.url
    save_data(data)
    return {"short_id": short_id, "short_url": f"http://{HOST}:{PORT}/{short_id}"}

@app.get("/hello")
def root():
    return {"message": "Welcome to the Simple URL shortener server!"}

@app.get("/{short_id}")
def expand_url(short_id: str):
    data = load_data()
    if short_id not in data:
        raise HTTPException(status_code=404, detail="URL not found")
    return RedirectResponse(url=data[short_id])

@app.delete("/{short_id}")
def delete_url(short_id: str):
    data = load_data()
    if short_id not in data:
        raise HTTPException(status_code=404, detail="URL not found")
    del data[short_id]
    save_data(data)
    return {"status": "deleted", "short_id": short_id}

@app.post("/monitor")
def trigger_monitoring():
    if not CLUSTER_NAME or not MONITOR_TASK_DEF_ARN:
        raise HTTPException(status_code=500, detail="Monitoring configuration missing")

    ecs = boto3.client("ecs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    try:
        response = ecs.run_task(
            cluster=CLUSTER_NAME,
            taskDefinition=MONITOR_TASK_DEF_ARN,
            launchType="EC2",
        )
        return {"status": "Monitoring task triggered", "failures": response.get("failures", [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
