import unittest

from bowxt.panels import validate_panel_document


class PanelProtocolTests(unittest.TestCase):
    def test_tree_protocol_normalizes_supported_fields(self):
        value = validate_panel_document({
            "version": 1,
            "type": "tree",
            "empty_text": "暂无",
            "nodes": [{
                "id": "group:1",
                "label": "群聊",
                "meta": "1 个会话",
                "expanded": True,
                "children": [{"label": "消息", "value": "你好", "tone": "info"}],
            }],
        })
        self.assertEqual(value["nodes"][0]["children"][0]["value"], "你好")

    def test_tree_protocol_rejects_executable_or_unknown_content(self):
        with self.assertRaisesRegex(ValueError, "accept only"):
            validate_panel_document({
                "version": 1,
                "type": "tree",
                "nodes": [{"label": "x", "html": "<script>alert(1)</script>"}],
            })


if __name__ == "__main__":
    unittest.main()
