import os
import time
from typing import List, Dict, Any, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

META_API_VERSION = os.getenv("META_API_VERSION", "v25.0")
API_KEY = os.getenv("APP_API_KEY", "change-me")
TOKENS = [t.strip() for t in os.getenv("META_TOKENS", "").split(",") if t.strip()]

app = FastAPI(title="BM Verification Checker")


class CheckRequest(BaseModel):
    business_ids: List[str]


class CheckItem(BaseModel):
    business_id: str
    name: Optional[str] = None
    verification_status: Optional[str] = None
    result: str
    detail: Optional[str] = None
    token_index: Optional[int] = None


def normalize_bm_id(raw: str) -> str:
    return "".join(ch for ch in str(raw).strip() if ch.isdigit())


async def fetch_business(client: httpx.AsyncClient, business_id: str, token: str) -> Dict[str, Any]:
    url = f"https://graph.facebook.com/{META_API_VERSION}/{business_id}"
    params = {
        "fields": "id,name,verification_status",
        "access_token": token,
    }
    r = await client.get(url, params=params, timeout=20.0)
    data = r.json()
    return {"status_code": r.status_code, "data": data}


def classify_meta_response(resp: Dict[str, Any]) -> Dict[str, Any]:
    status_code = resp["status_code"]
    data = resp["data"]

    if "verification_status" in data:
        return {
            "matched": True,
            "result": data["verification_status"],
            "name": data.get("name"),
            "verification_status": data.get("verification_status"),
            "detail": None,
            "retry_with_next_token": False,
        }

    if "error" in data:
        msg = data["error"].get("message", "Unknown error")
        code = data["error"].get("code")

        # token die / invalid
        if code == 190:
            return {
                "matched": False,
                "result": "TOKEN_EXPIRED",
                "name": None,
                "verification_status": None,
                "detail": msg,
                "retry_with_next_token": True,
            }

        # permission / object inaccessible
        if code in (10, 100, 200):
            return {
                "matched": False,
                "result": "NO_ACCESS",
                "name": None,
                "verification_status": None,
                "detail": msg,
                "retry_with_next_token": True,
            }

        return {
            "matched": False,
            "result": "ERROR",
            "name": None,
            "verification_status": None,
            "detail": f"HTTP {status_code} | {msg}",
            "retry_with_next_token": True,
        }

    # có id/name nhưng không có verification_status
    if "id" in data and "name" in data and "verification_status" not in data:
        return {
            "matched": False,
            "result": "NO_ACCESS",
            "name": data.get("name"),
            "verification_status": None,
            "detail": "Token đọc được BM cơ bản nhưng không đọc được verification_status",
            "retry_with_next_token": True,
        }

    return {
        "matched": False,
        "result": "ERROR",
        "name": data.get("name"),
        "verification_status": None,
        "detail": f"HTTP {status_code} | {data}",
        "retry_with_next_token": True,
    }


@app.get("/health")
async def health():
    return {"ok": True, "tokens": len(TOKENS)}


@app.post("/check-bm", response_model=List[CheckItem])
async def check_bm(req: CheckRequest, x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not TOKENS:
        raise HTTPException(status_code=500, detail="No META_TOKENS configured")

    results: List[CheckItem] = []

    async with httpx.AsyncClient() as client:
        for raw_id in req.business_ids:
            business_id = normalize_bm_id(raw_id)
            if not business_id:
                results.append(CheckItem(
                    business_id=str(raw_id),
                    result="ERROR",
                    detail="BM ID không hợp lệ"
                ))
                continue

            final_item = None

            for idx, token in enumerate(TOKENS, start=1):
                try:
                    resp = await fetch_business(client, business_id, token)
                    parsed = classify_meta_response(resp)

                    if parsed["matched"]:
                        final_item = CheckItem(
                            business_id=business_id,
                            name=parsed["name"],
                            verification_status=parsed["verification_status"],
                            result=parsed["result"],
                            detail=parsed["detail"],
                            token_index=idx
                        )
                        break

                except Exception as e:
                    parsed = {
                        "result": "ERROR",
                        "name": None,
                        "verification_status": None,
                        "detail": str(e),
                        "retry_with_next_token": True,
                    }

                # thử token tiếp theo
                time.sleep(0.15)

            if final_item is None:
                final_item = CheckItem(
                    business_id=business_id,
                    result="NO_ACCESS",
                    detail="Không token nào đọc được verification_status"
                )

            results.append(final_item)

    return results
