import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = PROJECT_ROOT / "extension"


def test_manifest_has_minimal_permissions() -> None:
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 3
    assert set(manifest["permissions"]) == {"activeTab", "scripting", "storage"}
    assert manifest["host_permissions"] == ["http://127.0.0.1/*"]
    assert not any("jd.com" in pattern for pattern in manifest["host_permissions"])


def test_popup_script_keeps_collection_narrow() -> None:
    script = (EXTENSION_DIR / "popup.js").read_text(encoding="utf-8")

    assert "https://search.jd.com/" in script
    assert "http://127.0.0.1:8765/v1/collect" in script
    assert "chrome.scripting.executeScript" in script
    assert "#J_goodsList li.gl-item" in script

    forbidden = (
        "document.cookie",
        "chrome.cookies",
        "localStorage",
        "sessionStorage",
        "outerHTML",
        ".innerHTML",
        "webRequest",
    )
    for marker in forbidden:
        assert marker not in script

