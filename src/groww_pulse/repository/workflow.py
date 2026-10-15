import json

from groww_pulse.errors import ErrorCode, PulseError


def save_run(conn, state):
    conn.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                 "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, "
                 "reason_code=excluded.reason_code, last_node=excluded.last_node, "
                 "state_json=excluded.state_json",
                 (state["run_id"], state["snapshot_id"], state["config_hash"], state["mode"],
                  state["status"], state.get("reason_code"), state.get("last_completed_node", "start"),
                  json.dumps(state)))


def get_run(conn, run_id):
    row = conn.execute("SELECT state_json FROM runs WHERE run_id=?", (str(run_id),)).fetchone()
    if not row:
        raise PulseError(ErrorCode.RUN_NOT_FOUND)
    return json.loads(row[0])


def put_result(conn, run_id, node, value):
    conn.execute("INSERT INTO node_results VALUES (?, ?, ?) ON CONFLICT(run_id,node) "
                 "DO UPDATE SET result_json=excluded.result_json", (run_id, node, json.dumps(value)))


def get_result(conn, run_id, node):
    row = conn.execute("SELECT result_json FROM node_results WHERE run_id=? AND node=?",
                       (run_id, node)).fetchone()
    return json.loads(row[0]) if row else None
