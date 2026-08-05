import json
from pathlib import Path

BASE = Path(__file__).parent
JSON_PATH = BASE / "legal_directory.json"
TEMPLATE_PATH = BASE / "legal_directory_template.html"
OUT_PATH = BASE / "legal_directory.html"


def main():
    data = json.loads(JSON_PATH.read_text())
    blob = json.dumps(data, ensure_ascii=False).replace("</script>", "<\\/script>")
    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    out = tpl.replace("__DATA_JSON__", blob)
    OUT_PATH.write_text(out, encoding="utf-8")
    print(f"wrote {OUT_PATH.name} ({OUT_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
