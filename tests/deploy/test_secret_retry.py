"""Regression for utility Vault writes replacing a secret's first field."""
import importlib.util
from pathlib import Path


path = Path(__file__).resolve().parents[2] / "ansible/plugins/filter/aiq_secrets.py"
spec = importlib.util.spec_from_file_location("aiq_secrets", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_old_secret_file_preserves_password_under_put_patch_protocol():
    old_file = {"secrets": [{"name": "model-reader", "fields": [
        {"name": "AWS_ACCESS_KEY_ID", "value": "reader"},
        {"name": "AWS_SECRET_ACCESS_KEY", "onMissingValue": "generate"},
    ]}]}
    stored = {"AWS_ACCESS_KEY_ID": "reader", "AWS_SECRET_ACCESS_KEY": "existing-password"}
    normalized = module.preserve_generated_secrets(old_file)
    # Exercise the utility's protocol: skip existing generated fields, then use
    # put for field zero and patch for every subsequent field.
    for index, field in enumerate(normalized["secrets"][0]["fields"]):
        name = field["name"]
        if field.get("onMissingValue") == "generate" and name in stored:
            continue
        value = field.get("value", "new-password")
        if index == 0:
            stored = {name: value}
        else:
            stored[name] = value
    assert stored["AWS_SECRET_ACCESS_KEY"] == "existing-password"
    assert old_file["secrets"][0]["fields"][0]["name"] == "AWS_ACCESS_KEY_ID"


def test_normalization_preserves_explicit_rotation_and_other_secret_values():
    original = {"bootstrap_secrets": [{"name": "bootstrap", "fields": []}], "secrets": [
        {"name": "database", "fields": [
            {"name": "username", "value": "aiq"},
            {"name": "password", "onMissingValue": "generate", "override": True},
        ]},
        {"name": "api", "fields": [{"name": "key", "value": "fixture"}]},
    ]}
    result = module.preserve_generated_secrets(original)
    assert result["secrets"][0]["fields"][0]["override"] is True
    assert result["secrets"][1] == original["secrets"][1]
    assert result["bootstrap_secrets"] == original["bootstrap_secrets"]
