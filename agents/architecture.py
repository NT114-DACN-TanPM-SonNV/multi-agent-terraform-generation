"""Agent 1 — Architecture: prompt → JSON plan (resources + data_sources).

Re-prompt in-node nếu plan có defect cấu trúc. Reset val_eng/deploy_eng/sec sau
mỗi re-plan vì code cũ không còn liên quan với lỗi mới.
"""
import logging

from core.state import AgentState
from core.llm import call_llm
from core.errors import make_fail
from core.parsers import parse_llm_json
from core.retry_control import new_tracker
from prompts.architecture import SYSTEM_PROMPT, DEFECT_FIX, ARCH_FIX_HEADER, ARCH_PREV_ATTEMPTS

logger = logging.getLogger(__name__)


def _parse_plan(raw: str) -> dict:
    """Parse JSON từ LLM response thành plan dict.

    Chỉ require 'resources' là list. 'data_sources' default [] nếu LLM bỏ qua.
    setdefault attributes/blocks ở đây vì A2/A3 subscript 2 key này trực tiếp.
    """
    plan = parse_llm_json(raw, {"resources": list})
    if not isinstance(plan.get("data_sources"), list):
        plan["data_sources"] = []
    for section in ("resources", "data_sources"):
        for obj in plan.get(section, []):
            if isinstance(obj, dict):
                obj.setdefault("attributes", {})
                obj.setdefault("blocks", {})
    return plan


def _plan_defects(plan: dict) -> list[str]:
    """Kiểm tra structure của plan — báo lỗi để LLM tự sửa, không drop âm thầm.

    Messages viết bằng English vì được đút vào prompt LLM.
    """
    defects: list[str] = []
    if not plan.get("resources"):
        defects.append("'resources' is empty — no infrastructure to generate")
        return defects
    for section in ("resources", "data_sources"):
        seen: set[str] = set()
        for i, obj in enumerate(plan.get(section, [])):
            if not isinstance(obj, dict):
                defects.append(f"{section}[{i}] is not a JSON object")
                continue
            t, n = obj.get("type"), obj.get("name")
            if not t or not n:
                missing = "type" if not t else "name"
                defects.append(f"{section}[{i}] is missing '{missing}'")
                continue
            label = f"{t}.{n}"
            if label in seen:
                defects.append(f"{section} declares '{label}' more than once")
            seen.add(label)
    return defects


def architecture_node(state: AgentState) -> dict:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": state["prompt"]},
    ]

    fix_feedback = state["fix_feedback"]
    fix_instruction = fix_feedback.get("fix_instruction", "")
    if fix_instruction and fix_feedback.get("root_cause") == "architecture":
        fix_msg = ARCH_FIX_HEADER.format(fix_instruction=fix_instruction)
        past = [e.get("fix_instruction", "")[:400]
                for e in state["arch_error_history"][-2:]
                if e.get("fix_instruction") and e.get("fix_instruction") != fix_instruction]
        if past:
            fix_msg += ARCH_PREV_ATTEMPTS + "\n".join(f"- {p}" for p in past)
        messages.append({"role": "user", "content": fix_msg})
    elif fix_instruction:
        logger.debug("Archi: fix_instruction ignored (root_cause=%s)", fix_feedback.get("root_cause"))

    try:
        raw = call_llm(messages, agent="architecture")
        plan = _parse_plan(raw)
    except Exception as e:
        return make_fail("INFRASTRUCTURE", None, f"Archi agent error: {e}")

    defects = _plan_defects(plan)
    if defects:
        logger.warning("Archi agent: %d defect — re-prompt: %s", len(defects), defects[:5])
        retry_msgs = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": DEFECT_FIX.format(
                defects="\n".join(f"- {d}" for d in defects))},
        ]
        try:
            raw = call_llm(retry_msgs, agent="architecture")
            plan = _parse_plan(raw)
        except Exception as e:
            return make_fail("INFRASTRUCTURE", None, f"Archi agent retry error: {e}")
        defects = _plan_defects(plan)
        if defects:
            return make_fail("INFRASTRUCTURE", None, f"Plan still has defects after retry: {defects[:3]}")

    logger.info("Archi agent: %d resources, %d data_sources",
                len(plan["resources"]), len(plan["data_sources"]))

    # Reset val_eng/deploy_eng/sec — lỗi cũ không còn liên quan sau re-plan.
    # val_arch/deploy_arch không reset (budget vòng re-plan).
    out: dict = {
        "infrastructure_plan": plan,
        "fix_feedback": {},
        "retries": {
            **state["retries"],
            "val_eng":    new_tracker(),
            "deploy_eng": new_tracker(),
            "sec":        new_tracker(),
        },
    }
    if fix_instruction and fix_feedback.get("root_cause") == "architecture":
        out["arch_error_history"] = (state["arch_error_history"] + [{"fix_instruction": fix_instruction}])[-5:]
    return out
