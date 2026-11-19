"""Agent Bridge v2 - Blender <-> CodeBuddy CLI local bridge.

Fixes the two fatal flaws of v1:
  1. The prompt is passed via stdin, never concatenated into the Windows
     command line (v1 died with "路径太长" on a 18 KB selection context).
  2. A strong system prompt, compact context and recent-result history replace
     the offline keyword rules that silently did nothing.

Usage:
  start_bridge.bat                 run the bridge
  start_bridge.bat --doctor        check CLI / paths / addon, then a tiny CLI call
  start_bridge.bat --selftest      simulate one full request without Blender
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

DEFAULT_CONFIG = {
    "shared_dir": "",
    "blender_version": "",
    "poll_interval": 0.4,
    "history_size": 3,
    "cli": {
        "command": "",
        "model": "",
        "timeout": 300,
        "permission_mode": "auto",
        "system_prompt_file": os.path.join(HERE, "system_prompt.txt"),
    },
    "agent": {
        "provider": "codebuddy",
        "command_template": "",
        "output_format": "stream-json",
        "stdin_mode": "none",
    },
}

DENY_TOKENS = (
    "os.system", "subprocess", "shutil.rmtree", "__import__", "eval(",
    "exec(", "open(", "quit_blender", "read_factory_settings",
    "save_userpref", "bpy.ops.wm.read_factory",
)

SHARED_DIR = ""
REQUEST_PATH = ""
RESPONSE_PATH = ""
RESULT_PATH = ""
PROGRESS_PATH = ""
HEARTBEAT_PATH = ""
STEPS_PATH = ""
CONTROL_PATH = ""
LOG_PATH = ""
_last_progress_write = 0.0
_last_heartbeat = 0.0
_last_steps = []


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    line = "[%s] %s" % (now(), message)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def blender_config_root():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, "Blender Foundation", "Blender")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "blender")


def suggest_blender_version():
    override = os.environ.get("BLENDER_VERSION", "").strip()
    if override:
        return override
    root = blender_config_root()
    versions = []
    if os.path.isdir(root):
        for name in os.listdir(root):
            if re.match(r"^\d+\.\d+$", name):
                versions.append(name)
    versions.sort(key=lambda item: tuple(int(part) for part in item.split(".")))
    return versions[-1] if versions else "4.2"


def suggest_shared_dir(version):
    return os.path.join(blender_config_root(), version, "scripts", "addons", "agent_bridge")


def load_config(path):
    with open(path, "r", encoding="utf-8-sig") as handle:
        config = json.load(handle)
    for key, value in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = value
        elif isinstance(value, dict):
            for sub_key, sub_value in value.items():
                config[key].setdefault(sub_key, sub_value)
    if not config.get("blender_version"):
        config["blender_version"] = suggest_blender_version()
    if not config.get("shared_dir"):
        config["shared_dir"] = suggest_shared_dir(config["blender_version"])
    prompt_file = config["cli"].get("system_prompt_file", "")
    if prompt_file and not os.path.isabs(prompt_file):
        config["cli"]["system_prompt_file"] = os.path.join(HERE, prompt_file)
    config["_path"] = path
    return config


def configure(config):
    global SHARED_DIR, REQUEST_PATH, RESPONSE_PATH, RESULT_PATH, PROGRESS_PATH, HEARTBEAT_PATH, STEPS_PATH, CONTROL_PATH, LOG_PATH
    SHARED_DIR = os.path.abspath(os.path.expandvars(config["shared_dir"]))
    os.makedirs(SHARED_DIR, exist_ok=True)
    REQUEST_PATH = os.path.join(SHARED_DIR, "request.json")
    RESPONSE_PATH = os.path.join(SHARED_DIR, "response.json")
    RESULT_PATH = os.path.join(SHARED_DIR, "result.json")
    PROGRESS_PATH = os.path.join(SHARED_DIR, "progress.json")
    HEARTBEAT_PATH = os.path.join(SHARED_DIR, "heartbeat.json")
    STEPS_PATH = os.path.join(SHARED_DIR, "steps.json")
    CONTROL_PATH = os.path.join(SHARED_DIR, "control.json")
    LOG_PATH = os.path.join(SHARED_DIR, "bridge.log")


def clear_progress():
    try:
        os.remove(PROGRESS_PATH)
    except OSError:
        pass


def clear_steps():
    try:
        os.remove(STEPS_PATH)
    except OSError:
        pass


def write_steps(request_id, steps):
    if not STEPS_PATH:
        return
    atomic_write_json(STEPS_PATH, {"id": request_id, "steps": steps})


def write_heartbeat():
    global _last_heartbeat
    if not HEARTBEAT_PATH or time.time() - _last_heartbeat < 2:
        return
    try:
        atomic_write_json(HEARTBEAT_PATH, {"pid": os.getpid(), "time": time.time()})
        _last_heartbeat = time.time()
    except Exception:
        pass


def heartbeat_loop():
    while True:
        write_heartbeat()
        time.sleep(1)


def acquire_single_instance_lock():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 9877))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


def push_progress(updates, kind, text):
    global _last_progress_write
    text = text.strip()
    if not text:
        return
    updates.append({
        "kind": kind,
        "text": text,
        "time": datetime.now().strftime("%H:%M:%S"),
    })
    print("[%s] %s" % (kind, text), flush=True)
    if PROGRESS_PATH and time.time() - _last_progress_write > 0.7:
        atomic_write_json(PROGRESS_PATH, {"updates": updates[-40:]})
        _last_progress_write = time.time()


_write_warning_count = 0


def atomic_write_json(path, payload, retries=8, delay=0.06):
    """Write JSON without ever raising on Windows file contention.

    Blender may have steps.json/progress.json open for reading while the bridge
    is writing. A failed replace is retried briefly and then skipped; a lost
    progress update is harmless, an interrupted agent request is not.
    """
    global _write_warning_count
    if not path:
        return False
    tmp = "%s.%d.%d.%d.tmp" % (path, os.getpid(), threading.get_ident(), time.time_ns())
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except Exception as exc:
        _write_warning_count += 1
        if _write_warning_count <= 3 or _write_warning_count % 20 == 0:
            log("写入临时文件失败 %s: %s" % (os.path.basename(path), exc))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    for attempt in range(retries):
        try:
            os.replace(tmp, path)
            return True
        except (PermissionError, OSError) as exc:
            if attempt == retries - 1:
                _write_warning_count += 1
                if _write_warning_count <= 3 or _write_warning_count % 20 == 0:
                    log("替换文件被占用 %s: %s" % (os.path.basename(path), exc))
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return False
            time.sleep(delay * (attempt + 1))
    return False


def read_json(path, retries=5, delay=0.05):
    last_error = None
    for attempt in range(retries):
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                return json.load(handle)
        except (PermissionError, OSError, ValueError) as exc:
            last_error = exc
            time.sleep(delay * (attempt + 1))
    raise last_error


def is_paused():
    if not CONTROL_PATH or not os.path.isfile(CONTROL_PATH):
        return False
    try:
        return bool(read_json(CONTROL_PATH, retries=2).get("paused", False))
    except Exception:
        return False


def wait_while_paused():
    while is_paused():
        time.sleep(0.25)


def resolve_cli(config):
    agent = config.get("agent", {})
    if str(agent.get("provider", "codebuddy")).lower() != "codebuddy":
        template = str(agent.get("command_template", "")).strip()
        executable = template.split()[0] if template else ""
        executable = os.path.expandvars(executable).strip('"')
        return shutil.which(executable) or executable

    command = os.path.expandvars(str(config["cli"].get("command", ""))).strip('"')
    if command and os.path.isfile(command):
        return command
    if command:
        found = shutil.which(command)
        if found:
            return found
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    patterns = [
        os.path.join(home, ".workbuddy", "binaries", "node", "versions", "*", "codebuddy.cmd"),
        os.path.join(home, ".codebuddy", "bin", "codebuddy.cmd"),
        os.path.join(home, ".codebuddy", "bin", "codebuddy"),
        os.path.join(home, ".local", "bin", "codebuddy"),
        os.path.join(os.environ.get("LOCALAPPDATA", home), "Programs", "CodeBuddy", "codebuddy.cmd"),
    ]
    for pattern in patterns:
        candidates = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        if candidates:
            return candidates[0]
    for name in ("codebuddy.cmd", "codebuddy"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def compact_selection(selection, limit=24):
    result = []
    for obj in (selection or [])[:limit]:
        result.append({
            "name": obj.get("name"),
            "type": obj.get("type"),
            "dimensions": obj.get("dimensions"),
            "location": obj.get("location"),
            "scale": obj.get("scale"),
            "bounds_min": obj.get("bounds_min"),
            "bounds_max": obj.get("bounds_max"),
            "vertices": obj.get("vertices"),
            "polygons": obj.get("polygons"),
            "materials": obj.get("materials"),
            "modifiers": obj.get("modifiers"),
        })
    if len(selection or []) > limit:
        result.append({"_truncated": "还有 %d 个对象未展开" % (len(selection) - limit)})
    return result


def compact_edit(edit):
    if not edit:
        return None
    return {
        "object": edit.get("object"),
        "vertices": (edit.get("vertices") or [])[:64],
        "edges": (edit.get("edges") or [])[:64],
        "faces": (edit.get("faces") or [])[:64],
    }


def build_prompt(request, history):
    payload = {
        "instruction": request.get("instruction", ""),
        "mode": request.get("mode"),
        "active_object": request.get("active_object"),
        "selection": compact_selection(request.get("selection")),
        "edit_selection": compact_edit(request.get("edit_selection")),
        "scene": request.get("scene", {}),
        "scene_objects": (request.get("scene_objects") or [])[:200],
        "materials": (request.get("materials") or [])[:100],
        "collections": (request.get("collections") or [])[:100],
        "blender_version": request.get("blender_version"),
        "blend_file": request.get("blend_file"),
    }
    parts = [
        "用户指令：" + str(request.get("instruction", "")).strip(),
        "",
        "场景上下文（JSON）：",
        json.dumps(payload, ensure_ascii=False),
    ]
    if request.get("screenshot"):
        parts.extend([
            "",
            "视口截图路径：" + str(request["screenshot"]) + "（可以用 Read 工具打开查看当前画面）",
        ])
    if history:
        lines = []
        for item in history:
            lines.append("- %s -> %s (%s)" % (
                item.get("instruction", ""),
                item.get("explanation", ""),
                "已应用" if item.get("applied") else "未应用",
            ))
        parts.extend(["", "最近几次修改（保持风格连续，不要重复已完成的改动）："] + lines)
    parts.extend([
        "",
        "请分步骤完成，不要等全部想完才动手。格式：每一步先用一行 # 思考: <这步要做什么>，",
        "紧接着输出一个 ```python 代码块，只写这一步的补丁代码。写完一步就进入下一步。",
        "每步代码都要能独立执行，并且 import bpy。",
    ])
    return "\n".join(parts)


def build_agent_command(prompt, config, cli):
    agent = config.get("agent", {})
    provider = str(agent.get("provider", "codebuddy")).lower()
    cli_config = config["cli"]
    system_prompt_file = cli_config.get("system_prompt_file", "")

    if provider == "codebuddy":
        args = [
            cli, "-p", "--output-format", "stream-json", "--include-partial-messages",
            "--permission-mode", str(cli_config.get("permission_mode", "auto")),
            "--no-session-persistence",
            "--tools", "Read",
            "--add-dir", os.path.abspath(os.path.expandvars(config["shared_dir"])),
        ]
        if system_prompt_file and os.path.isfile(system_prompt_file):
            args.extend(["--system-prompt-file", system_prompt_file])
        if cli_config.get("model"):
            args.extend(["--model", str(cli_config["model"])])
        return subprocess.list2cmdline(args), prompt, True

    template = str(agent.get("command_template", "")).strip()
    if not template:
        raise RuntimeError("agent.command_template 未配置")
    prompt_file = os.path.join(SHARED_DIR or PROJECT, "prompt.txt")
    with open(prompt_file, "w", encoding="utf-8") as handle:
        handle.write(prompt)
    command_line = template.format(
        prompt_file=prompt_file,
        system_prompt_file=system_prompt_file,
        shared_dir=SHARED_DIR or PROJECT,
    )
    stdin_text = prompt if str(agent.get("stdin_mode", "none")).lower() == "prompt" else ""
    stream_json = str(agent.get("output_format", "text")).lower() == "stream-json"
    return command_line, stdin_text, stream_json


def call_cli(prompt, config, cli, request_id=None):
    global _last_steps
    cli_config = config["cli"]
    command_line, stdin_text, stream_json = build_agent_command(prompt, config, cli)
    proc = subprocess.Popen(
        command_line,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=PROJECT,
        shell=True,
    )
    try:
        if stdin_text:
            proc.stdin.write(stdin_text)
        proc.stdin.close()
    except Exception:
        pass

    updates = []
    buffers = {"思考": "", "输出": ""}
    text_all = ""
    final_text = ""
    steps = []
    emitted = 0
    thought_buf = ""

    def flush_blocks():
        nonlocal emitted, thought_buf
        blocks = re.findall(r"```(?:python|py)?\s*(.*?)```", text_all, re.S | re.I)
        while emitted < len(blocks):
            code = blocks[emitted].strip()
            emitted += 1
            marker = code.find("import bpy")
            if marker > 0:
                code = code[marker:]
            code = code.strip()
            valid, message = validate_patch(code)
            if not valid:
                push_progress(updates, "错误", "第 %d 步补丁被拒绝: %s" % (emitted, message))
                thought_buf = ""
                continue
            thought = re.sub(r"```.*?```", "", thought_buf, flags=re.S).strip()
            thought = re.sub(r"#\s*思考[:：]\s*", "", thought).strip()
            thought = " ".join(thought.split())
            thought_buf = ""
            steps.append({"index": len(steps) + 1, "thought": thought, "patch": code})
            write_steps(request_id, steps)
            push_progress(updates, "操作", "第 %d 步已生成：%s" % (
                len(steps), (thought or "执行建模操作")[:70]
            ))

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if is_paused():
                if not thought_buf:
                    thought_buf = "[继续生成]"
                if not stream_json:
                    text_all += line + "\n"
                else:
                    try:
                        paused_event = json.loads(line)
                        delta = paused_event.get("event", {}).get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            text_all += delta["text"]
                    except Exception:
                        pass
                wait_while_paused()
                continue
            if not stream_json:
                text_all += line + "\n"
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("type") == "stream_event":
                body = event.get("event", {})
                if body.get("type") == "content_block_delta":
                    delta = body.get("delta", {})
                    kind = None
                    chunk = None
                    if delta.get("type") == "thinking_delta":
                        kind, chunk = "思考", delta.get("thinking")
                    elif delta.get("type") == "text_delta":
                        kind, chunk = "输出", delta.get("text")
                        if chunk:
                            text_all += chunk
                    if kind and chunk:
                        buffers[kind] += chunk
                        if kind == "输出":
                            thought_buf += chunk
                        if "\n" in chunk or len(buffers[kind]) >= 60:
                            push_progress(updates, kind, buffers[kind])
                            buffers[kind] = ""
                        if kind == "输出":
                            flush_blocks()
            elif event.get("type") == "result":
                final_text = event.get("result", "") or ""
        if not stream_json:
            final_text = text_all
            push_progress(updates, "输出", text_all[:500])
        flush_blocks()
        for kind in ("思考", "输出"):
            if buffers[kind]:
                push_progress(updates, kind, buffers[kind])
    finally:
        try:
            stderr = proc.stderr.read()
        except Exception:
            stderr = ""
        try:
            returncode = proc.wait(timeout=float(cli_config.get("timeout", 300)))
        except Exception:
            proc.kill()
            returncode = -1
    if returncode != 0:
        raise RuntimeError("CLI 退出码 %s: %s" % (returncode, (stderr or "")[:600]))
    _last_steps = steps
    return final_text or text_all


def extract_patch(text):
    note_match = re.search(r"#\s*说明[:：]\s*(.+)", text)
    note = note_match.group(1).strip() if note_match else ""
    fenced = re.search(r"```(?:python|py)?\s*(.*?)```", text, re.S | re.I)
    code = fenced.group(1) if fenced else text
    marker = code.find("import bpy")
    if marker > 0:
        code = code[marker:]
    return code.strip(), note


def validate_patch(code):
    lowered = code.lower()
    for token in DENY_TOKENS:
        if token in lowered:
            return False, "包含被禁止的调用: " + token
    if "bpy." not in lowered:
        return False, "补丁里没有 bpy 操作"
    try:
        compile(code, "<agent_patch>", "exec")
    except SyntaxError as exc:
        return False, "语法错误: " + str(exc)
    return True, "ok"


def process_request(request, config, history, cli):
    global _last_steps
    instruction = str(request.get("instruction", "")).strip()
    log("收到请求 %s: %s" % (request.get("id"), instruction))
    clear_progress()
    clear_steps()
    _last_steps = []
    push_progress([], "状态", "已收到指令，正在读取场景上下文并调用模型…")
    prompt = build_prompt(request, history)
    try:
        raw = call_cli(prompt, config, cli, request.get("id"))
        patch, note = extract_patch(raw)
        if not patch:
            raise RuntimeError("CLI 没有返回代码")
        valid, message = validate_patch(patch)
        if not valid:
            raise RuntimeError("补丁校验失败: " + message)
        explanation = note or ("由 CodeBuddy CLI 生成补丁：" + instruction[:60])
        status = "done"
    except Exception as exc:
        log("生成失败: %s" % exc)
        patch = ""
        explanation = str(exc)[:600]
        status = "error"

    response = {
        "id": request.get("id"),
        "status": status,
        "patch": patch,
        "steps": len(_last_steps) > 0,
        "step_count": len(_last_steps),
        "explanation": explanation,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    atomic_write_json(RESPONSE_PATH, response)
    push_progress([], "状态", "共 %d 步已写入，Blender 正在逐步应用： %s" % (
        len(_last_steps), explanation[:100]
    ))
    history.append({
        "instruction": instruction[:120],
        "explanation": explanation[:120],
        "applied": status == "done",
    })
    max_history = int(config.get("history_size", 3))
    if len(history) > max_history:
        del history[:-max_history]
    log("已写入响应 %s 状态=%s" % (request.get("id"), status))
    return response


def run_doctor(config, cli):
    print("=== Agent Bridge Doctor ===")
    print("CLI       :", cli or "未找到")
    print("共享目录  :", os.path.abspath(os.path.expandvars(config["shared_dir"])))
    prompt_file = config["cli"].get("system_prompt_file", "")
    print("系统提示词:", prompt_file, "存在" if os.path.isfile(prompt_file) else "缺失")
    version = str(config.get("blender_version") or suggest_blender_version())
    base = os.path.join(blender_config_root(), version)
    candidates = [
        os.path.join(base, "scripts", "addons", "blender_agent_console.py"),
        os.path.join(base, "scripts", "addons", "agent_console", "__init__.py"),
        os.path.join(base, "extensions", "agent_console", "__init__.py"),
    ]
    installed = next((path for path in candidates if os.path.isfile(path)), None)
    print("Blender插件:", installed or candidates[0], "已安装" if installed else "未找到")
    if not cli:
        print("[FAIL] 找不到 CodeBuddy CLI")
        return 1
    print("\n试调用 CLI（stdin 传提示词）...")
    try:
        doctor_config = json.loads(json.dumps(config))
        doctor_config["cli"]["system_prompt_file"] = ""
        answer = call_cli("只输出 OK，不要输出其它内容。", doctor_config, cli).strip()
        print("CLI 返回:", repr(answer[:200]))
        if "OK" not in answer:
            print("[FAIL] CLI 没有返回预期内容")
            return 1
    except Exception as exc:
        print("[FAIL]", exc)
        return 1
    print("[OK] 桥接所需的 CLI 与文件均可用")
    return 0


def run_selftest(config, cli, instruction="把选中对象的颜色改成红色"):
    configure(config)
    global RESPONSE_PATH
    tmp_dir = os.path.join(SHARED_DIR, "_selftest")
    os.makedirs(tmp_dir, exist_ok=True)
    RESPONSE_PATH = os.path.join(tmp_dir, "response.json")
    request = {
        "id": "selftest_" + datetime.now().strftime("%H%M%S"),
        "instruction": instruction,
        "mode": "OBJECT",
        "active_object": "TestCube",
        "selection": [{
            "name": "TestCube", "type": "MESH", "dimensions": [1, 1, 1],
            "location": [0, 0, 0], "scale": [1, 1, 1],
            "bounds_min": [-0.5, -0.5, 0], "bounds_max": [0.5, 0.5, 1],
            "vertices": 8, "polygons": 6,
            "materials": ["Mat"], "modifiers": [],
        }],
        "edit_selection": None,
        "scene": {"name": "Scene", "camera": "Camera", "render_engine": "BLENDER_EEVEE"},
        "blend_file": "selftest.blend",
        "status": "pending",
    }
    response = process_request(request, config, [], cli)
    print("status:", response["status"])
    print("explanation:", response["explanation"])
    print("patch:\n" + (response["patch"] or "(空)"))
    return 0 if response["status"] == "done" else 1


def main():
    parser = argparse.ArgumentParser()
    default_config = os.path.join(HERE, "config.json")
    if not os.path.isfile(default_config):
        default_config = os.path.join(HERE, "config.example.json")
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--instruction", default="把选中对象的颜色改成红色", help="自检用的指令")
    parser.add_argument("--once", action="store_true", help="处理一个待处理请求后退出")
    args = parser.parse_args()

    config = load_config(args.config)
    cli = resolve_cli(config)
    if args.doctor:
        return run_doctor(config, cli)
    if not cli:
        print("找不到 CodeBuddy CLI，请先运行 --doctor")
        return 1
    if args.selftest:
        return run_selftest(config, cli, args.instruction)

    lock_socket = None
    if not args.once:
        lock_socket = acquire_single_instance_lock()
        if lock_socket is None:
            print("已有桥接服务在运行，本次退出。")
            return 0

    configure(config)
    history = []
    last_id = None
    log("Agent Bridge v2 已启动，CLI=" + os.path.basename(cli))
    if not args.once:
        threading.Thread(target=heartbeat_loop, daemon=True).start()
    if args.once:
        for _ in range(200):
            if os.path.isfile(REQUEST_PATH):
                request = read_json(REQUEST_PATH)
                if request.get("status") == "pending":
                    process_request(request, config, history, cli)
                    return 0
            time.sleep(0.25)
        print("等待请求超时")
        return 1

    while True:
        try:
            if is_paused():
                time.sleep(0.25)
                continue
            if os.path.isfile(REQUEST_PATH):
                request = read_json(REQUEST_PATH)
                request_id = request.get("id")
                status = request.get("status")
                if status == "pending" and request_id != last_id:
                    process_request(request, config, history, cli)
                    last_id = request_id
                elif request_id == last_id and status in ("applied", "failed"):
                    last_id = None
            time.sleep(float(config.get("poll_interval", 0.4)))
        except KeyboardInterrupt:
            log("已停止")
            return 0
        except Exception:
            log("桥接异常:\n" + traceback.format_exc())
            time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
