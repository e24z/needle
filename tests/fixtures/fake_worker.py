import json
import os
import sys
import time


MODE = os.environ.get("FAKE_WORKER_MODE", "normal")
BACKEND = os.environ.get("FAKE_WORKER_BACKEND", "fake-soft-lamr")

loaded = False


def write(response):
    print(json.dumps(response, separators=(",", ":")), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    op = request.get("op")

    if MODE == "hang" and op == "load":
        time.sleep(60)
        continue

    if MODE == "die" and op == "load":
        write({"id": request_id, "ok": True, "status": "resident", "backend": BACKEND})
        sys.exit(0)

    if op == "status":
        status = "resident" if loaded else "cold"
        response = {"id": request_id, "ok": True, "status": status}
        if loaded:
            response["backend"] = BACKEND
    elif op == "load":
        loaded = True
        response = {"id": request_id, "ok": True, "status": "resident", "backend": BACKEND}
    elif op == "prune":
        loaded = True
        text = request["text"]
        if MODE == "slow" or "SLOW" in text:
            time.sleep(1.5)
        pruned = text.replace(" drop", "")
        decision = "pruned" if pruned != text else "unchanged"
        reason = "model" if decision == "pruned" else "no-lines-removed"
        response = {
            "id": request_id,
            "ok": True,
            "status": "resident",
            "backend": BACKEND,
            "decision": decision,
            "reason": reason,
            "text": pruned,
            "stats": {
                "decision": decision,
                "reason": reason,
                "input_chars": len(text),
                "output_chars": len(pruned),
                "saved_chars": max(0, len(text) - len(pruned)),
            },
        }
    elif op == "unload":
        if MODE == "counting-unload":
            with open(os.environ["NEEDLE_UNLOAD_LOG"], "a", encoding="utf-8") as handle:
                handle.write("unload\n")
            time.sleep(0.2)
        loaded = False
        response = {"id": request_id, "ok": True, "status": "cold"}
    elif op == "exit":
        loaded = False
        response = {"id": request_id, "ok": True, "status": "cold"}
        write(response)
        break
    else:
        response = {
            "id": request_id,
            "ok": False,
            "status": "failed",
            "error": "bad op",
        }
    write(response)
