from wechatrebot_manager import cli


class _GbkStream:
    encoding = "gbk"

    def __init__(self) -> None:
        self.output = ""

    def write(self, value: str) -> int:
        value.encode(self.encoding)
        self.output += value
        return len(value)


def test_json_output_escapes_characters_missing_from_gbk(monkeypatch) -> None:
    stream = _GbkStream()
    monkeypatch.setattr(cli.sys, "stdout", stream)

    cli._print_json({"message": "思考 ✍"}, indent=2)

    assert '"message": "\\u601d\\u8003 \\u270d"' in stream.output
