"""Preserve generated Vault fields with the utility loader's put/patch protocol."""
from copy import deepcopy


def preserve_generated_secrets(document):
    result = deepcopy(document)
    for secret in result.get("secrets", []):
        fields = secret.get("fields", [])
        fields.sort(key=lambda field: field.get("onMissingValue") != "generate")
    return result


class FilterModule:
    def filters(self):
        return {"aiq_preserve_generated_secrets": preserve_generated_secrets}
