import json
import os
import sys


audit_path = os.environ["FAKE_CODEX_AUDIT"]
mode = os.environ.get("FAKE_CODEX_MODE", "normal")
thread_id = "codex-thread"
turn_id = "codex-turn"


def send(message: dict) -> None:
    print(json.dumps(message, ensure_ascii=False), flush=True)


def receive() -> dict:
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    message = json.loads(line)
    with open(audit_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(message, ensure_ascii=False) + "\n")
    return message


def reply(message: dict, result: dict) -> None:
    send({"id": message["id"], "result": result})


while True:
    message = receive()
    method = message.get("method")
    if method == "initialize":
        reply(message, {"protocolVersion": 1})
    elif method == "initialized":
        pass
    elif method == "thread/start":
        if mode == "early-fatal":
            send({"method": "error", "params": {
                "message": "authentication failed", "willRetry": False,
            }})
        elif mode == "early-foreign-fatal":
            send({"method": "error", "params": {
                "message": "another thread failed", "willRetry": False,
                "threadId": "foreign-thread", "turnId": "foreign-turn",
            }})
        elif mode == "early-retryable":
            send({"method": "error", "params": {
                "message": "temporary transport failure", "willRetry": True,
            }})
        reply(message, {"thread": {"id": thread_id}})
    elif method == "thread/resume":
        resumed = message["params"]["threadId"]
        thread_id = resumed
        reply(message, {"thread": {"id": "wrong-thread" if mode == "wrong-resume" else resumed}})
    elif method == "turn/start":
        reply(message, {"turn": {"id": turn_id}})
        common = {"threadId": thread_id, "turnId": turn_id, "itemId": "item-1"}
        if mode == "permission":
            send({
                "id": "permission-1",
                "method": "item/commandExecution/requestApproval",
                "params": {**common, "command": "write output.txt"},
            })
            while receive().get("id") != "permission-1":
                pass
        if mode == "secret":
            send({
                "id": "secret-1",
                "method": "item/tool/requestUserInput",
                "params": {**common, "questions": [{"id": "token", "isSecret": True, "question": "token"}]},
            })
            while receive().get("id") != "secret-1":
                pass
            # A rejected secret prompt must abort the turn immediately; the
            # fake server deliberately never emits turn/completed here.
            continue
        if mode == "hang":
            send({"method": "item/started", "params": {
                **common, "item": {"type": "commandExecution"},
            }})
            continue
        send({"method": "item/reasoning/summaryTextDelta", "params": {**common, "delta": "思考"}})
        send({"method": "item/started", "params": {**common, "item": {"type": "commandExecution"}}})
        send({"method": "item/agentMessage/delta", "params": {**common, "delta": "完成"}})
        send({"method": "turn/completed", "params": {
            **common, "turn": {"id": turn_id, "status": "completed"},
        }})
    elif method == "turn/interrupt":
        reply(message, {})
    elif "id" in message:
        reply(message, {})
