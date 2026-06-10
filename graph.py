"""LangGraph pipeline — ráp 5 agent thành StateGraph với các vòng retry.

Topology:
    START → architecture → security → engineering → validation

    validation ─(route_after_validation)─→ deployment        (pass)
                                         → architecture       (MISSING_RESOURCE)
                                         → engineering        (SYNTAX/LOGIC/SECURITY)
                                         → requires_human     (INFRASTRUCTURE/budget)

    deployment ─(route_after_deployment)─→ END                (success)
                                         → engineering        (LOGIC — code fix)
                                         → architecture       (MISSING_RESOURCE — re-plan)
                                         → requires_human     (INFRASTRUCTURE/dirty/budget/OTHER)

Edge tĩnh:
  architecture → security   (A1 thành công luôn sang A2)
  security → engineering    (A2 không có failure path — fail trả profile rỗng, không dừng)

Conditional edge sau A1 và A3: chặn INFRASTRUCTURE fail khỏi chảy xuống
gây A4 chấm code rỗng → loop oan. Xem route_after_architecture, route_after_engineering.

TRANSIENT retry (network/throttle) của A5 xảy ra IN-NODE (vòng lặp 2 lần bên trong
deployment_node) — không cần edge riêng vì không thay đổi state giữa các lần thử.
"""
import logging
from langgraph.graph import StateGraph, START, END
from core.state import AgentState
from core.retry_control import new_tracker
from agents.architecture import architecture_node
from agents.security import security_node
from agents.engineering import engineering_node
from agents.validation import validation_node, route_after_validation
from agents.deployment import deployment_node, route_after_deployment

logger = logging.getLogger(__name__)

# Cao hơn default 25 vì các vòng retry (mỗi cycle 2-5 node) có thể vượt 25 trước khi
# chạm cap. Hai backstop độc lập theo pha (validation total_val_attempts=5 + deploy
# total_deploy_attempts=4) cho phép node count cao hơn cap chung total=5 cũ → nâng margin.
# Worst-case (A4 5 retry + A5 4 retry, xen kẽ re-plan) ~70 node → 150 cho margin;
# các cap retry thật mới là chốt chặn loop, RECURSION_LIMIT chỉ là trần an toàn.
RECURSION_LIMIT = 150


def route_after_architecture(state: AgentState) -> str:
    fb = state.get("fix_feedback") or {}
    if fb.get("error_type") == "INFRASTRUCTURE":
        return "requires_human"
    return "security"


def route_after_engineering(state: AgentState) -> str:
    fb = state.get("fix_feedback") or {}
    if not fb.get("error_type"):
        return "validation"
    return "requires_human"


def requires_human_node(state: AgentState) -> dict:
    """Terminal: pipeline cần can thiệp người. Lý do nằm trong fix_feedback/
    deployment_result. Không đổi state."""
    vr = state.get("fix_feedback") or {}
    dr = state.get("deployment_result") or {}
    logger.info("REQUIRES_HUMAN — validation=%s deployment=%s",
                vr.get("fix_instruction"), dr.get("error_type"))
    return {}


def build_graph():
    """Dựng và compile LangGraph StateGraph cho toàn pipeline."""
    g = StateGraph(AgentState)

    g.add_node("architecture", architecture_node)
    g.add_node("security", security_node)
    g.add_node("engineering", engineering_node)
    g.add_node("validation", validation_node)
    g.add_node("deployment", deployment_node)
    g.add_node("requires_human", requires_human_node)

    g.add_edge(START, "architecture")
    g.add_conditional_edges("architecture", route_after_architecture, {
        "security": "security",
        "requires_human": "requires_human",
    })
    g.add_edge("security", "engineering")
    g.add_conditional_edges("engineering", route_after_engineering, {
        "validation": "validation",
        "requires_human": "requires_human",
    })
    g.add_conditional_edges("validation", route_after_validation, {
        "deployment":     "deployment",
        "architecture":   "architecture",
        "engineering":    "engineering",
        "requires_human": "requires_human",
    })
    g.add_conditional_edges("deployment", route_after_deployment, {
        "end": END,
        "engineering": "engineering",
        "architecture": "architecture",
        "requires_human": "requires_human",
    })
    g.add_edge("requires_human", END)

    return g.compile()


def build_initial_state(prompt: str) -> AgentState:
    """Khởi tạo đầy đủ AgentState — TypedDict không có default, thiếu field → KeyError."""
    state: AgentState = {
        "prompt": prompt,
        "infrastructure_plan": {},
        "security_profile": {},
        "security_status": "ok",
        "generated_code": "",
        "fix_feedback": {},
        "deployment_result": {},
        "retries": {
            "val_eng":    new_tracker(),
            "val_arch":   new_tracker(),
            "deploy_eng": new_tracker(),
            "deploy_arch": new_tracker(),
            "sec":        new_tracker(),
        },
        "total_val_attempts": 0,
        "total_deploy_attempts": 0,
        "routing_log": [],
        "arch_error_history": [],
        "eng_error_history": [],
        "run_dir": "",
    }
    # Guard: thêm field vào AgentState mà quên init ở đây → lỗi rõ ràng tại init,
    # không phải KeyError mơ hồ tận trong node giữa pipeline.
    missing = set(AgentState.__annotations__) - set(state)
    if missing:
        raise KeyError(f"build_initial_state thiếu field: {sorted(missing)}")
    return state


# Compile một lần khi import — tái dùng cho mọi lần invoke
graph = build_graph()


def run_pipeline(prompt: str, **kwargs) -> AgentState:
    """Chạy toàn pipeline trên một prompt, trả final state."""
    initial = build_initial_state(prompt, **kwargs)
    return graph.invoke(initial, config={"recursion_limit": RECURSION_LIMIT})


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = sys.argv[1] if len(sys.argv) > 1 else \
        "Create an S3 bucket with versioning and server-side encryption enabled."
    final = run_pipeline(p)
    print("\n" + "=" * 60)
    print(f"PROMPT: {p}")
    print(f"resources: {len(final['infrastructure_plan'].get('resources', []))}")
    prof = final.get("security_profile") or {}
    print(f"sec_checks: {sum(len(v.get('checks',[])) for v in prof.values())} total IDs selected")
    print(f"code chars: {len(final['generated_code'])}")
    print(f"validation: {final['fix_feedback'].get('overall_passed')} "
          f"({final['fix_feedback'].get('error_type')})")
    print(f"deployment: {final['deployment_result'].get('success')} "
          f"({final['deployment_result'].get('error_type')})")
    _retries = final.get("retries") or {}
    _deploy_retry = (_retries.get("deploy_eng", {}).get("count", 0) +
                     _retries.get("deploy_arch", {}).get("count", 0))
    print(f"total_val_attempts: {final.get('total_val_attempts', 0)}  "
          f"total_deploy_attempts: {final.get('total_deploy_attempts', 0)}  deploy_retry: {_deploy_retry}")
    print(f"routing_log: {len(final['routing_log'])} entries")
