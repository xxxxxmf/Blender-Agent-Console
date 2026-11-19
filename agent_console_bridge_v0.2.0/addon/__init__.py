bl_info = {
    "name": "Agent Console Bridge",
    "author": "CodeBuddy",
    "version": (1, 1, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > Agent Console",
    "description": "Capture Blender selection, send it to a local agent bridge, and apply returned bpy patches with undo support.",
    "category": "Development",
}

import bpy
import bmesh
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime
from mathutils import Vector, Matrix, Quaternion

def default_bridge_dir():
    try:
        scripts_dir = bpy.utils.user_resource("SCRIPTS", path="addons")
        return os.path.join(scripts_dir, "agent_bridge")
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".blender_agent_bridge")


BRIDGE_DIR_DEFAULT = default_bridge_dir()
REQUEST_NAME = "request.json"
RESPONSE_NAME = "response.json"
RESULT_NAME = "result.json"
PROGRESS_NAME = "progress.json"
STEPS_NAME = "steps.json"
HEARTBEAT_NAME = "heartbeat.json"


def default_bridge_service():
    """Locate the bridge script through installer marker or relative layout."""
    here = os.path.dirname(os.path.abspath(__file__))
    marker = os.path.join(here, "bridge_path.txt")
    if os.path.isfile(marker):
        try:
            with open(marker, "r", encoding="utf-8") as handle:
                recorded = handle.read().strip()
            if recorded and os.path.isfile(recorded):
                return recorded
        except Exception:
            pass
    candidates = [
        os.path.join(os.path.dirname(here), "bridge", "agent_bridge.py"),
        os.path.join(here, "bridge", "agent_bridge.py"),
    ]
    return next((path for path in candidates if os.path.isfile(path)), "")

DANGEROUS_TOKENS = (
    "os.system",
    "subprocess",
    "shutil.rmtree",
    "__import__",
    "eval(",
    "exec(",
    "open(",
    "quit_blender",
    "read_factory_settings",
    "save_userpref",
)

SYSTEM_PROMPT = """你是一个 Blender bpy 脚本生成器。
只输出可执行的 Python 代码，不要输出解释、注释块或 Markdown。
运行时可用变量：bpy, context, selection, selected_names, edit_selection, request,
以及 bmesh, math, random, Vector, Matrix, Quaternion。
你可以对场景做任何 Blender 操作：新建/删除物体、改网格、材质、修改器、灯光、相机、动画、场景设置。
禁止调用系统命令、读写外部文件、quit_blender、read_factory_settings、save_userpref。"""

CONNECTION_MODE_ITEMS = (
    ("BRIDGE", "本地桥接", "通过 request.json/response.json 连接本地桥接服务"),
    ("API", "直接 API", "在插件内直接调用 OpenAI 兼容 API"),
)

BRIDGE_AGENT_ITEMS = (
    ("CODEBUDDY_CLI", "CodeBuddy CLI", "调用本机 CodeBuddy CLI Agent，保留专用会话上下文"),
    ("OFFLINE", "离线规则", "无需 API，使用内置规则处理常见修改"),
    ("OPENAI", "OpenAI 兼容桥接", "由桥接服务调用 config.json 中配置的 API"),
    ("CUSTOM", "自定义命令", "由桥接服务调用 config.json 中配置的自定义 Agent 命令"),
)

_last_request = None
_timer_handle = None
_applied_steps = {}


def get_prefs():
    addon = bpy.context.preferences.addons.get(__name__)
    return addon.preferences if addon else None


def get_bridge_dir():
    prefs = get_prefs()
    if prefs and getattr(prefs, "bridge_dir", ""):
        return prefs.bridge_dir
    return BRIDGE_DIR_DEFAULT


def bridge_connected():
    try:
        path = os.path.join(get_bridge_dir(), HEARTBEAT_NAME)
        if not os.path.isfile(path):
            return False
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return time.time() - float(data.get("time", 0)) < 12
    except Exception:
        return False


def get_console_props():
    scene = bpy.context.scene
    return getattr(scene, "agent_console_bridge", None)


def object_info(obj):
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    bmin = [round(min(c[i] for c in corners), 4) for i in range(3)]
    bmax = [round(max(c[i] for c in corners), 4) for i in range(3)]
    materials = [slot.material.name for slot in obj.material_slots if slot.material]
    return {
        "name": obj.name,
        "type": obj.type,
        "collections": [collection.name for collection in obj.users_collection],
        "location": [round(v, 4) for v in obj.location],
        "rotation": [round(v, 4) for v in obj.rotation_euler],
        "scale": [round(v, 4) for v in obj.scale],
        "dimensions": [round(v, 4) for v in obj.dimensions],
        "bounds_min": bmin,
        "bounds_max": bmax,
        "vertices": len(obj.data.vertices) if obj.type == "MESH" else 0,
        "polygons": len(obj.data.polygons) if obj.type == "MESH" else 0,
        "materials": materials,
        "modifiers": [mod.name + ":" + mod.type for mod in obj.modifiers],
        "parent": obj.parent.name if obj.parent else None,
    }


def edit_selection_info(context):
    obj = context.view_layer.objects.active
    if not obj or obj.type != "MESH" or obj.mode != "EDIT":
        return None
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.verts.index_update()
    bm.edges.index_update()
    bm.faces.index_update()
    return {
        "object": obj.name,
        "vertices": [v.index for v in bm.verts if v.select],
        "edges": [e.index for e in bm.edges if e.select],
        "faces": [f.index for f in bm.faces if f.select],
    }


def viewport_shot(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    with bpy.context.temp_override(window=window, area=area):
                        bpy.ops.screen.screenshot_area(filepath=path)
                    return path
    except Exception:
        pass
    return None


def build_request(context, instruction):
    selected = list(context.selected_objects)
    active = context.view_layer.objects.active
    screenshot = viewport_shot(os.path.join(get_bridge_dir(), "viewport.png"))
    return {
        "id": datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "instruction": instruction,
        "mode": context.mode,
        "active_object": active.name if active else None,
        "selection_names": [obj.name for obj in selected],
        "selection": [object_info(obj) for obj in selected],
        "edit_selection": edit_selection_info(context),
        "scene": {
            "name": context.scene.name,
            "camera": context.scene.camera.name if context.scene.camera else None,
            "render_engine": context.scene.render.engine,
        },
        "scene_objects": [
            {
                "name": obj.name,
                "type": obj.type,
                "location": [round(v, 3) for v in obj.location],
                "dimensions": [round(v, 3) for v in obj.dimensions],
            }
            for obj in context.scene.objects
        ][:200],
        "materials": [mat.name for mat in bpy.data.materials],
        "collections": [col.name for col in bpy.data.collections],
        "blender_version": bpy.app.version_string,
        "screenshot": screenshot,
        "blend_file": bpy.data.filepath,
        "status": "pending",
    }


def validate_patch(patch):
    lowered = patch.lower()
    for token in DANGEROUS_TOKENS:
        if token in lowered:
            return False, "补丁包含被禁止的调用: " + token
    if "import bpy" not in lowered and "bpy." not in lowered:
        return False, "补丁没有包含 bpy 操作"
    return True, "ok"


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def clean_api_patch(content):
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:python)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    return content.strip()


def api_call(request, prefs):
    base_url = getattr(prefs, "api_base_url", "").strip()
    api_key = getattr(prefs, "api_key", "").strip()
    model = getattr(prefs, "api_model", "gpt-4o-mini").strip()
    if not base_url or not api_key:
        return {
            "id": request.get("id"),
            "status": "error",
            "patch": "",
            "explanation": "直接 API 模式需要填写 Base URL 和 API Key。",
        }

    endpoint = base_url if base_url.endswith("/chat/completions") else base_url.rstrip("/") + "/chat/completions"
    user_payload = {
        "instruction": request.get("instruction", ""),
        "mode": request.get("mode"),
        "active_object": request.get("active_object"),
        "selection": request.get("selection", []),
        "edit_selection": request.get("edit_selection"),
        "scene": request.get("scene", {}),
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "temperature": 0.2,
    }
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
    }
    try:
        req = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
        content = result["choices"][0]["message"]["content"]
        return {
            "id": request.get("id"),
            "status": "done",
            "patch": clean_api_patch(content),
            "explanation": "由 " + model + " 直接生成补丁。",
        }
    except Exception as exc:
        return {
            "id": request.get("id"),
            "status": "error",
            "patch": "",
            "explanation": "API 调用失败: " + str(exc),
        }


def apply_agent_response(response, request):
    props = get_console_props()
    prefs = get_prefs()
    explanation = response.get("explanation", "")
    status = response.get("status", "error")
    applied = False
    message = explanation

    if status == "done" and response.get("steps"):
        applied = True
        message = "Agent 已完成全部 %s 步操作" % response.get("step_count", "")
    elif status == "done":
        patch = response.get("patch", "")
        valid, check_message = validate_patch(patch)
        if not valid:
            message = "拒绝执行补丁: " + check_message
        elif prefs and not prefs.auto_apply:
            message = "已接收响应，自动应用已关闭。补丁预览:\n" + patch[:400]
        else:
            try:
                selection_objects = [
                    bpy.data.objects.get(name)
                    for name in request.get("selection_names", [])
                ]
                selection_objects = [obj for obj in selection_objects if obj]
                namespace = {
                    "bpy": bpy,
                    "bmesh": bmesh,
                    "math": math,
                    "random": random,
                    "Vector": Vector,
                    "Matrix": Matrix,
                    "Quaternion": Quaternion,
                    "context": bpy.context,
                    "selection": selection_objects,
                    "selected_names": request.get("selection_names", []),
                    "edit_selection": request.get("edit_selection"),
                    "request": request,
                }
                bpy.ops.ed.undo_push(message="Agent Console: " + explanation[:60])
                exec(compile(patch, "<agent_patch>", "exec"), namespace, namespace)
                applied = True
                message = "已应用: " + explanation
            except Exception:
                message = "补丁执行失败:\n" + traceback.format_exc()
    else:
        message = "Agent 返回错误: " + explanation

    if props:
        props.status = message[:120]
        props.last_response = message[:1000]
        append_chat(props, "Agent: " + message)

    if request is not None:
        request["status"] = "applied" if applied else "failed"
        request["applied"] = applied
        request["result_message"] = message[:500]
        if prefs and prefs.connection_mode == "BRIDGE":
            write_json(os.path.join(get_bridge_dir(), REQUEST_NAME), request)

    write_json(os.path.join(get_bridge_dir(), RESULT_NAME), {
        "id": response.get("id"),
        "applied": applied,
        "message": message,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    })
    return applied, message


def apply_step_patch(step, request):
    props = get_console_props()
    patch = step.get("patch", "")
    thought = step.get("thought", "")
    index = step.get("index", "?")
    valid, check = validate_patch(patch)
    if not valid:
        message = "第 %s 步被拒绝: %s" % (index, check)
        if props:
            append_chat(props, "Agent[拒绝]: " + message)
        return False, message
    try:
        selection_objects = [
            bpy.data.objects.get(name) for name in request.get("selection_names", [])
        ]
        selection_objects = [obj for obj in selection_objects if obj]
        namespace = {
            "bpy": bpy,
            "bmesh": bmesh,
            "math": math,
            "random": random,
            "Vector": Vector,
            "Matrix": Matrix,
            "Quaternion": Quaternion,
            "context": bpy.context,
            "selection": selection_objects,
            "selected_names": request.get("selection_names", []),
            "edit_selection": request.get("edit_selection"),
            "request": request,
        }
        bpy.ops.ed.undo_push(message="Agent step %s" % index)
        exec(compile(patch, "<agent_step>", "exec"), namespace, namespace)
        message = "第 %s 步完成：%s" % (index, (thought or "已执行")[:80])
        if props:
            append_chat(props, "Agent: " + message)
        return True, message
    except Exception:
        message = "第 %s 步失败:\n%s" % (index, traceback.format_exc()[:400])
        if props:
            append_chat(props, "Agent[错误]: " + message)
        return False, message


def poll_steps():
    global _applied_steps
    if _last_request is None:
        return
    request_id = _last_request.get("id")
    path = os.path.join(get_bridge_dir(), STEPS_NAME)
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return
    if data.get("id") != request_id:
        return
    steps = data.get("steps", [])
    applied = _applied_steps.get(request_id, 0)
    for step in steps[applied:]:
        ok, message = apply_step_patch(step, _last_request)
        applied += 1
        _applied_steps[request_id] = applied
        props = get_console_props()
        if props:
            props.status = message[:120]
            props.last_response = message[:1000]
        if not ok:
            break


def poll_progress(props):
    path = os.path.join(get_bridge_dir(), PROGRESS_NAME)
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        updates = data.get("updates", [])
        if not updates:
            return
        lines = [
            "[%s] %s" % (item.get("kind", "进度"), item.get("text", ""))
            for item in updates[-12:]
        ]
        props.last_response = "\n".join(lines)[:1000]
        last = updates[-1]
        if last.get("kind") in ("状态", "操作", "错误"):
            props.status = ("[%s] %s" % (
                last.get("kind", "进度"), last.get("text", "")
            ))[:120]
    except Exception:
        pass


def poll_agent_response():
    global _last_request
    prefs = get_prefs()
    if prefs and prefs.connection_mode != "BRIDGE":
        return 0.5
    props = get_console_props()
    if props is None or _last_request is None:
        return 0.5

    poll_progress(props)
    poll_steps()

    response_path = os.path.join(get_bridge_dir(), RESPONSE_NAME)
    if not os.path.isfile(response_path):
        sent_time = _last_request.get("_sent_time")
        if sent_time and time.time() - sent_time > 300 and not _last_request.get("_warned_no_response"):
            props.status = "桥接服务响应较慢，请稍候（模型生成通常需要 1-2 分钟）"
            _last_request["_warned_no_response"] = True
        return 0.5

    try:
        with open(response_path, "r", encoding="utf-8") as handle:
            response = json.load(handle)
    except Exception as exc:
        props.status = "响应读取失败: " + str(exc)
        return 0.5

    if response.get("id") != _last_request.get("id"):
        return 0.5

    apply_agent_response(response, _last_request)

    try:
        os.remove(response_path)
    except OSError:
        pass
    return 0.5


class AgentConsolePreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    connection_mode: bpy.props.EnumProperty(
        name="Connection Mode",
        description="How the console reaches an agent",
        items=CONNECTION_MODE_ITEMS,
        default="BRIDGE",
    )
    bridge_agent_mode: bpy.props.EnumProperty(
        name="Bridge Agent",
        description="Which agent the local bridge service should use",
        items=BRIDGE_AGENT_ITEMS,
        default="CODEBUDDY_CLI",
    )
    bridge_dir: bpy.props.StringProperty(
        name="Bridge Directory",
        description="Directory shared with the local agent bridge service",
        default=BRIDGE_DIR_DEFAULT,
        subtype="DIR_PATH",
    )
    bridge_service: bpy.props.StringProperty(
        name="Bridge Service Script",
        description="Full path to bridge/agent_bridge.py (installer sets this automatically)",
        default=default_bridge_service(),
        subtype="FILE_PATH",
    )
    api_base_url: bpy.props.StringProperty(
        name="API Base URL",
        description="OpenAI-compatible endpoint base, e.g. https://api.example.com/v1",
        default="",
    )
    api_key: bpy.props.StringProperty(
        name="API Key",
        description="Stored in Blender user preferences, not in the .blend file",
        default="",
        subtype="PASSWORD",
    )
    api_model: bpy.props.StringProperty(
        name="Model",
        description="Model name sent to the API",
        default="gpt-4o-mini",
    )
    auto_apply: bpy.props.BoolProperty(
        name="Auto Apply Response",
        description="Automatically execute returned bpy patches",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "connection_mode")
        if self.connection_mode == "BRIDGE":
            layout.prop(self, "bridge_agent_mode")
            layout.prop(self, "bridge_dir")
            layout.prop(self, "bridge_service")
            layout.label(text="Bridge files: request.json / steps.json / heartbeat.json", icon="INFO")
        else:
            layout.prop(self, "api_base_url")
            layout.prop(self, "api_key")
            layout.prop(self, "api_model")
            layout.label(text="Key stays in user preferences, not in the scene file.", icon="INFO")
        layout.prop(self, "auto_apply")


class AgentChatMessage(bpy.types.PropertyGroup):
    text: bpy.props.StringProperty(name="Text", default="")


def append_chat(props, text):
    if props is None or not text:
        return
    text = str(text).replace("\n", " ")[:500]
    item = props.messages.add()
    item.text = text
    while len(props.messages) > 200:
        props.messages.remove(0)
    props.active_message = len(props.messages) - 1


class AgentConsoleProps(bpy.types.PropertyGroup):
    instruction: bpy.props.StringProperty(
        name="给 Agent 的消息",
        description="Describe what the agent should do with the current scene",
        default="",
    )
    status: bpy.props.StringProperty(name="Status", default="空闲")
    last_response: bpy.props.StringProperty(name="Last Response", default="")
    messages: bpy.props.CollectionProperty(type=AgentChatMessage)
    active_message: bpy.props.IntProperty(name="Active Message", default=0)


class AGENT_CONSOLE_OT_send_selection(bpy.types.Operator):
    bl_idname = "agent_bridge.send_selection"
    bl_label = "发送消息"
    bl_description = "Send the captured selection to the configured agent connection"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        global _last_request, _applied_steps
        props = get_console_props()
        prefs = get_prefs()
        if not props or not props.instruction.strip():
            self.report({"WARNING"}, "请先输入消息")
            return {"CANCELLED"}

        message = props.instruction.strip()
        append_chat(props, "你: " + message)
        props.instruction = ""
        mode = prefs.connection_mode if prefs else "BRIDGE"
        _last_request = build_request(context, message)
        _last_request["connection_mode"] = mode
        _last_request["agent_mode"] = (
            prefs.bridge_agent_mode if prefs and mode == "BRIDGE" else "API"
        )
        _last_request["_sent_time"] = time.time()
        _last_request["_warned_no_response"] = False
        props.last_response = ""

        if mode == "API":
            props.status = "正在调用 API..."
            response = api_call(_last_request, prefs)
            applied, message = apply_agent_response(response, _last_request)
            self.report({"INFO" if applied else "WARNING"}, message[:160])
            return {"FINISHED"}

        bridge_dir = get_bridge_dir()
        response_path = os.path.join(bridge_dir, RESPONSE_NAME)
        result_path = os.path.join(bridge_dir, RESULT_NAME)
        progress_path = os.path.join(bridge_dir, PROGRESS_NAME)
        steps_path = os.path.join(bridge_dir, STEPS_NAME)
        for path in (response_path, result_path, progress_path, steps_path):
            if os.path.isfile(path):
                os.remove(path)
        _applied_steps[_last_request.get("id")] = 0

        write_json(os.path.join(bridge_dir, REQUEST_NAME), _last_request)
        props.status = "已发送，等待 Agent 思考..."
        self.report({"INFO"}, "请求已写入 " + bridge_dir)
        return {"FINISHED"}


class AGENT_CONSOLE_OT_test_api(bpy.types.Operator):
    bl_idname = "agent_bridge.test_api"
    bl_label = "测试 API 连接"
    bl_description = "Send a minimal request to verify the configured API endpoint"
    bl_options = {"REGISTER"}

    def execute(self, context):
        prefs = get_prefs()
        if not prefs:
            self.report({"WARNING"}, "插件偏好不可用")
            return {"CANCELLED"}
        base_url = prefs.api_base_url.strip()
        api_key = prefs.api_key.strip()
        if not base_url or not api_key:
            self.report({"WARNING"}, "请先填写 Base URL 和 API Key")
            return {"CANCELLED"}

        endpoint = base_url if base_url.endswith("/chat/completions") else base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": prefs.api_model,
            "messages": [{"role": "user", "content": "Reply with OK only."}],
        }
        payload = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        }
        try:
            req = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as response:
                json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            self.report({"ERROR"}, "API 测试失败: " + str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, "API 可用: " + prefs.api_model)
        return {"FINISHED"}


class AGENT_CONSOLE_OT_apply_response(bpy.types.Operator):
    bl_idname = "agent_bridge.apply_response"
    bl_label = "重新应用响应"
    bl_description = "Check for a pending bridge response immediately"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        message = poll_agent_response()
        props = get_console_props()
        if props:
            props.status = props.status or "没有待处理的响应"
        self.report({"INFO"}, props.status if props else "检查完成")
        return {"FINISHED"}


class AGENT_CONSOLE_OT_start_bridge(bpy.types.Operator):
    bl_idname = "agent_bridge.start_bridge"
    bl_label = "启动桥接服务"
    bl_description = "Start the local CodeBuddy bridge service in a new console window"
    bl_options = {"REGISTER"}

    def execute(self, context):
        prefs = get_prefs()
        script = (prefs.bridge_service if prefs else "") or default_bridge_service()
        if not script or not os.path.isfile(script):
            self.report({"ERROR"}, "找不到桥接脚本，请在插件偏好设置里指定 Bridge Service Script")
            return {"CANCELLED"}
        command = [sys.executable, script]
        options = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        try:
            subprocess.Popen(command, **options)
        except Exception as exc:
            self.report({"ERROR"}, "启动失败: " + str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, "桥接服务已启动，等待心跳…")
        return {"FINISHED"}


class AGENT_BRIDGE_UL_chat(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        item_icon = "USER" if str(item.text).startswith("你:") else "CONSOLE"
        layout.label(text=item.text, icon=item_icon)


class AGENT_BRIDGE_PT_panel(bpy.types.Panel):
    bl_label = "Agent Console"
    bl_idname = "AGENT_BRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Agent Console"

    def draw(self, context):
        layout = self.layout
        props = get_console_props()
        if not props:
            layout.label(text="Console properties unavailable", icon="ERROR")
            return
        prefs = get_prefs()
        connected = bridge_connected()

        header = layout.row(align=True)
        header.label(text="Agent 控制台", icon="CONSOLE")
        header.label(
            text="桥接在线" if connected else "桥接离线",
            icon="LINKED" if connected else "UNLINKED",
        )

        box = layout.box()
        box.label(text="桥接服务", icon="PLUGIN")
        row = box.row(align=True)
        row.operator("agent_bridge.start_bridge", text="启动 / 重启", icon="PLAY")
        row.operator("agent_bridge.apply_response", text="刷新", icon="FILE_REFRESH")
        box.label(text=("● 服务在线" if connected else "● 服务未启动"), icon="INFO")

        layout.separator()
        box = layout.box()
        box.label(text="与 Agent 对话", icon="CONSOLE")
        if len(props.messages) == 0:
            box.label(text="(还没有对话，输入一条消息开始)")
        box.template_list(
            "AGENT_BRIDGE_UL_chat", "",
            props, "messages", props, "active_message",
            rows=5,
        )
        box.separator()
        box.prop(props, "instruction", text="消息")
        row = box.row(align=True)
        row.operator("agent_bridge.send_selection", text="发送", icon="EXPORT")
        row.operator("agent_bridge.apply_response", text="刷新", icon="FILE_REFRESH")
        status = props.status or "空闲"
        if "错误" in status or "失败" in status:
            icon = "ERROR"
        elif "完成" in status or "已应用" in status:
            icon = "CHECKMARK"
        elif "等待" in status or "收到" in status or status.startswith("["):
            icon = "TIME"
        else:
            icon = "INFO"
        box.label(text="状态: " + status[:90], icon=icon)

        layout.separator()
        box = layout.box()
        box.label(text="选择上下文", icon="RESTRICT_SELECT_OFF")
        selected = list(context.selected_objects)
        if not selected:
            box.label(text="(未选择对象)")
        else:
            box.label(text="已选择 %d 个对象" % len(selected))
            for obj in selected[:6]:
                box.label(text="- " + obj.name)
            if len(selected) > 6:
                box.label(text="... 以及其他 %d 个" % (len(selected) - 6))

        layout.separator()
        if prefs:
            box = layout.box()
            box.label(text="Agent 连接方式（高级）", icon="PREFERENCES")
            box.prop(prefs, "connection_mode")
            if prefs.connection_mode == "BRIDGE":
                box.prop(prefs, "bridge_agent_mode")
                box.prop(prefs, "bridge_dir")
            else:
                box.prop(prefs, "api_base_url")
                box.prop(prefs, "api_key")
                box.prop(prefs, "api_model")
                box.operator("agent_bridge.test_api", icon="PLUGIN")


classes = (
    AgentConsolePreferences,
    AgentChatMessage,
    AgentConsoleProps,
    AGENT_CONSOLE_OT_send_selection,
    AGENT_CONSOLE_OT_test_api,
    AGENT_CONSOLE_OT_apply_response,
    AGENT_CONSOLE_OT_start_bridge,
    AGENT_BRIDGE_UL_chat,
    AGENT_BRIDGE_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.agent_console_bridge = bpy.props.PointerProperty(type=AgentConsoleProps)

    global _timer_handle
    if not bpy.app.timers.is_registered(poll_agent_response):
        _timer_handle = bpy.app.timers.register(poll_agent_response, persistent=True, first_interval=0.5)


def unregister():
    global _timer_handle
    if _timer_handle is not None and bpy.app.timers.is_registered(poll_agent_response):
        bpy.app.timers.unregister(poll_agent_response)
    _timer_handle = None

    del bpy.types.Scene.agent_console_bridge
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
