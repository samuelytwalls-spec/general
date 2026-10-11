import json
import os

from piagent.agent import Agent, Session, parse_action
from piagent.llm import LlamaClient
from piagent.rag import DocIndex


def make_agent(cfg, devices=None):
    llm = LlamaClient(cfg.llm)
    return Agent(cfg, llm, DocIndex(cfg.rag, llm), devices)


def test_safe_command_runs_without_confirmation(cfg, llama):
    llama.script = [
        {"tool": "shell", "input": "echo hello-from-shell"},
        {"tool": "final_answer", "input": "It printed hello."},
    ]
    agent = make_agent(cfg)
    res = agent.run_turn(Session(), "say hello")
    assert res["status"] == "done"
    assert res["answer"] == "It printed hello."
    results = [e for e in res["events"] if e["type"] == "tool_result"]
    assert "hello-from-shell" in results[0]["output"]
    # the request used a JSON-schema grammar and the observation reached the model
    second = llama.chat_requests()[1]
    assert second["response_format"]["type"] == "json_schema"
    assert "hello-from-shell" in second["messages"][-1]["content"]


def test_destructive_command_waits_for_approval(cfg, llama, tmp_path):
    os.makedirs(cfg.agent.workdir)
    victim = os.path.join(cfg.agent.workdir, "victim.txt")
    open(victim, "w").close()
    llama.script = [
        {"tool": "shell", "input": "rm victim.txt"},
        {"tool": "final_answer", "input": "Deleted."},
    ]
    agent = make_agent(cfg)
    sess = Session()
    res = agent.run_turn(sess, "delete victim.txt")
    assert res["status"] == "needs_confirmation"
    assert res["pending"]["input"] == "rm victim.txt"
    assert "deletes files" in " ".join(res["pending"]["reasons"])
    assert os.path.exists(victim), "must not run before approval"

    res = agent.resolve(sess, True)
    assert res["status"] == "done"
    assert not os.path.exists(victim)


def test_rejected_command_is_not_run_or_retried(cfg, llama):
    os.makedirs(cfg.agent.workdir)
    victim = os.path.join(cfg.agent.workdir, "keep.txt")
    open(victim, "w").close()
    llama.script = [
        {"tool": "shell", "input": "rm keep.txt"},
        {"tool": "shell", "input": "rm keep.txt"},  # model tries again
        {"tool": "final_answer", "input": "You declined."},
    ]
    agent = make_agent(cfg)
    sess = Session()
    assert agent.run_turn(sess, "delete keep.txt")["status"] == "needs_confirmation"
    res = agent.resolve(sess, False)
    assert res["status"] == "done"
    assert os.path.exists(victim)
    observations = [m["content"] for m in sess.messages if m["role"] == "user"]
    assert any("REJECTED" in o for o in observations)
    assert any("already rejected" in o for o in observations)


def test_denied_command_is_refused(cfg, llama):
    llama.script = [
        {"tool": "shell", "input": "sudo reboot"},
        {"tool": "final_answer", "input": "Not allowed."},
    ]
    res = make_agent(cfg).run_turn(Session(), "reboot")
    assert res["status"] == "done"
    assert any(e["type"] == "refused" for e in res["events"])


def test_step_limit_forces_final_answer(cfg, llama):
    cfg.agent.max_steps = 3
    llama.script = [{"tool": "shell", "input": "true"}] * 3 + [
        lambda body: {"tool": "final_answer", "input": "gave up",
                      "_enum": body["response_format"]["json_schema"]["schema"]["properties"]["tool"]["enum"]},
    ]
    res = make_agent(cfg).run_turn(Session(), "loop forever")
    assert res["status"] == "done" and res["answer"] == "gave up"
    last_schema = llama.chat_requests()[-1]["response_format"]["json_schema"]["schema"]
    assert last_schema["properties"]["tool"]["enum"] == ["final_answer"]


def test_search_docs_tool(cfg, llama):
    with open(os.path.join(cfg.rag.docs_dir, "router.md"), "w") as fh:
        fh.write("The router admin password is stored in the blue binder.")
    agent = make_agent(cfg)
    agent.docs.index()
    llama.script = [
        {"tool": "search_docs", "input": "router admin password"},
        {"tool": "final_answer", "input": "In the blue binder (router.md)."},
    ]
    res = agent.run_turn(Session(), "where is the router password?")
    out = [e for e in res["events"] if e["type"] == "tool_result"][0]["output"]
    assert "router.md" in out and "blue binder" in out


def test_context_is_trimmed_but_keeps_current_request(cfg, llama):
    cfg.agent.context_chars = 3000
    agent = make_agent(cfg)
    sess = Session()
    for i in range(20):
        sess.messages.append({"role": "user", "content": f"old message {i} " + "x" * 400})
    sess.messages.append({"role": "user", "content": "CURRENT REQUEST"})
    sess.turn_start = len(sess.messages) - 1
    prompt = agent._prompt(sess)
    assert prompt[0]["role"] == "system"
    assert any(m["content"] == "CURRENT REQUEST" for m in prompt)
    assert sum(len(m["content"]) for m in prompt) < 3000 + 600


def test_parse_action_tolerates_noise():
    assert parse_action('noise {"thought":"x","tool":"shell","input":"ls"} tail')["tool"] == "shell"
    assert parse_action("plain text answer") == {"thought": "", "tool": "final_answer", "input": "plain text answer"}
    assert parse_action(json.dumps({"tool": "bogus", "input": "x"}))["tool"] == "final_answer"


def test_truncated_json_is_retried_with_more_tokens(cfg, llama):
    llama.script = [
        {"_truncate": True},
        {"tool": "final_answer", "input": "complete"},
    ]
    res = make_agent(cfg).run_turn(Session(), "hi")
    assert res["answer"] == "complete"
    first, retry = llama.chat_requests()
    assert retry["max_tokens"] == first["max_tokens"] * 2
