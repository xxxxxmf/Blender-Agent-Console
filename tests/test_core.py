import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))

import agent_bridge as bridge  # noqa: E402


class CoreTests(unittest.TestCase):
    def test_validate_patch_accepts_bpy_code(self):
        ok, message = bridge.validate_patch("import bpy\nbpy.ops.object.select_all(action='DESELECT')")
        self.assertTrue(ok, message)

    def test_validate_patch_rejects_system_calls(self):
        ok, _ = bridge.validate_patch("import bpy\nimport os\nos.system('echo hi')")
        self.assertFalse(ok)

    def test_validate_patch_rejects_syntax_error(self):
        ok, _ = bridge.validate_patch("import bpy\nif True:")
        self.assertFalse(ok)

    def test_extract_patch_from_fenced_block(self):
        text = "# 说明: 测试\n```python\nimport bpy\nbpy.ops.object.select_all()\n```"
        patch, note = bridge.extract_patch(text)
        self.assertIn("import bpy", patch)
        self.assertIn("测试", note)

    def test_compact_selection_truncates(self):
        items = [{"name": "obj%d" % index} for index in range(40)]
        compacted = bridge.compact_selection(items, limit=5)
        self.assertEqual(len(compacted), 6)
        self.assertIn("_truncated", compacted[-1])

    def test_build_prompt_contains_instruction(self):
        prompt = bridge.build_prompt({"instruction": "生成一个球"}, [])
        self.assertIn("生成一个球", prompt)
        self.assertIn("# 思考", prompt)

    def test_example_config_loads_with_dynamic_defaults(self):
        config = bridge.load_config(str(ROOT / "bridge" / "config.example.json"))
        self.assertTrue(config["shared_dir"])
        self.assertTrue(config["blender_version"])
        self.assertTrue(os.path.isabs(config["cli"]["system_prompt_file"]))

    def test_suggest_blender_version_returns_string(self):
        self.assertIsInstance(bridge.suggest_blender_version(), str)

    def test_atomic_write_with_empty_path_does_not_raise(self):
        self.assertFalse(bridge.atomic_write_json("", {"a": 1}))

    def test_pause_control_roundtrip(self):
        import tempfile

        original = bridge.CONTROL_PATH
        with tempfile.TemporaryDirectory() as tmp:
            bridge.CONTROL_PATH = os.path.join(tmp, "control.json")
            try:
                self.assertFalse(bridge.is_paused())
                bridge.atomic_write_json(bridge.CONTROL_PATH, {"paused": True})
                self.assertTrue(bridge.is_paused())
            finally:
                bridge.CONTROL_PATH = original

    def test_generic_agent_command_template(self):
        import tempfile

        original = bridge.SHARED_DIR
        with tempfile.TemporaryDirectory() as tmp:
            bridge.SHARED_DIR = tmp
            try:
                config = bridge.load_config(str(ROOT / "bridge" / "config.example.json"))
                config["agent"] = {
                    "provider": "generic",
                    "command_template": "myagent --prompt-file {prompt_file} --system {system_prompt_file}",
                    "output_format": "text",
                    "stdin_mode": "none",
                }
                command, stdin_text, stream_json = bridge.build_agent_command("hello", config, "")
                self.assertIn("--prompt-file", command)
                self.assertIn("prompt.txt", command)
                self.assertEqual(stdin_text, "")
                self.assertFalse(stream_json)
                self.assertTrue((Path(tmp) / "prompt.txt").is_file())
            finally:
                bridge.SHARED_DIR = original


if __name__ == "__main__":
    unittest.main()
