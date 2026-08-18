#!/usr/bin/env python3
"""
AgnosticV Catalog Validator — standalone deterministic checker.

Re-implements 10 core checks from the agnosticv:validator skill's sub-agents
(schema-checker, metadata-checker) in plain Python so they can run without
LLM inference.  Zero external dependencies beyond PyYAML.

Usage:
    python agv_checker.py /path/to/catalog_directory
    python agv_checker.py /path/to/catalog_directory --json
"""

import json
import os
import re
import sys
from datetime import datetime, date

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML is required.  Install with:  pip install pyyaml")


def _vault_constructor(loader, node):
    """Treat !vault tagged values as plain strings (we don't decrypt them)."""
    return loader.construct_scalar(node)


yaml.SafeLoader.add_constructor("!vault", _vault_constructor)


VALID_CATEGORIES = {"Workshops", "Labs", "Demos", "Open_Environments", "Brand_Events"}

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

CREDENTIAL_KEYWORDS = {
    "password", "passwd", "secret", "token", "access_key", "api_key", "credential",
}
SKIP_SUFFIXES = {
    "_length", "_policy", "_type", "_format", "_expires",
    "_name", "_url", "_path", "_label",
}

HASH_BAD_PATTERNS = [
    re.compile(r"hash\(", re.IGNORECASE),
    re.compile(r"\bsha\b", re.IGNORECASE),
    re.compile(r"\bmd5\b", re.IGNORECASE),
    re.compile(r"\bguid\b.*hash", re.IGNORECASE),
    re.compile(r"\bpassword_hash\b", re.IGNORECASE),
    re.compile(r"\bb64encode\b", re.IGNORECASE),
    re.compile(r"sha256", re.IGNORECASE),
    re.compile(r"sha1\b", re.IGNORECASE),
]

GENERIC_KEYWORDS = {
    "workshop", "demo", "lab", "sandbox", "openshift", "ansible",
    "rhel", "tutorial", "training", "course", "test", "example",
}

EE_DATE_RE = re.compile(r"chained-(\d{4}-\d{2}-\d{2})")


# ---------------------------------------------------------------------------
# Result collector
# ---------------------------------------------------------------------------

class Results:
    def __init__(self, catalog_path: str):
        self.catalog_path = catalog_path
        self.errors: list[dict] = []
        self.warnings: list[dict] = []
        self.suggestions: list[dict] = []
        self.passed_checks: list[str] = []

    def add_error(self, check: str, message: str, **extra):
        entry = {"check": check, "severity": "ERROR", "message": message}
        entry.update(extra)
        self.errors.append(entry)

    def add_warning(self, check: str, message: str, **extra):
        entry = {"check": check, "severity": "WARNING", "message": message}
        entry.update(extra)
        self.warnings.append(entry)

    def add_suggestion(self, check: str, message: str, **extra):
        entry = {"check": check, "severity": "SUGGESTION", "message": message}
        entry.update(extra)
        self.suggestions.append(entry)

    def add_passed(self, message: str):
        self.passed_checks.append(message)

    def to_dict(self) -> dict:
        status = "passed"
        if self.errors:
            status = "failed"
        elif self.warnings:
            status = "passed_with_warnings"

        return {
            "status": status,
            "catalog_path": self.catalog_path,
            "errors": self.errors,
            "warnings": self.warnings,
            "suggestions": self.suggestions,
            "passed_checks": self.passed_checks,
            "summary": (
                f"{len(self.errors)} errors, "
                f"{len(self.warnings)} warnings, "
                f"{len(self.suggestions)} suggestions, "
                f"{len(self.passed_checks)} checks passed"
            ),
        }


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_yaml_syntax(catalog_path: str, results: Results):
    """Check 4: common.yaml must parse cleanly.  Returns parsed data or None."""
    common = os.path.join(catalog_path, "common.yaml")
    try:
        with open(common) as f:
            data = yaml.safe_load(f)
        results.add_passed("YAML syntax valid: common.yaml")
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError as exc:
        results.add_error("yaml_syntax", f"YAML syntax error in common.yaml: {str(exc)[:200]}")
        return None
    except FileNotFoundError:
        return None


def check_file_structure(catalog_path: str, results: Results):
    """Check 1: common.yaml required, dev.yaml and description.adoc recommended."""
    files = set(os.listdir(catalog_path))

    if "common.yaml" not in files:
        results.add_error("file_structure", "Missing required file: common.yaml")
        return False  # signal to stop further checks
    results.add_passed("Required file present: common.yaml")

    if "description.adoc" not in files:
        results.add_warning("file_structure", "Recommended file missing: description.adoc")
    else:
        results.add_passed("Recommended file present: description.adoc")

    if "dev.yaml" not in files:
        results.add_warning("file_structure", "Recommended file missing: dev.yaml")
    else:
        results.add_passed("Recommended file present: dev.yaml")

    return True


def check_uuid(data: dict, results: Results):
    """Check 2: __meta__.asset_uuid must exist and be a valid UUID."""
    meta = data.get("__meta__")
    if not isinstance(meta, dict):
        results.add_error("uuid_format", "Missing __meta__ section")
        return

    uuid_val = meta.get("asset_uuid")
    if uuid_val is None:
        results.add_error("uuid_format", "Missing __meta__.asset_uuid")
        return

    uuid_str = str(uuid_val)
    if not UUID_RE.match(uuid_str):
        results.add_error("uuid_format", f"Invalid UUID format: {uuid_str}")
        return

    results.add_passed(f"UUID format valid: {uuid_str}")


def check_category(data: dict, results: Results):
    """Check 3: __meta__.catalog.category must be a valid enum value."""
    meta = data.get("__meta__")
    if not isinstance(meta, dict):
        return  # already flagged by UUID check

    catalog = meta.get("catalog")
    if not isinstance(catalog, dict):
        results.add_error("category_validation", "Missing __meta__.catalog section")
        return

    category = catalog.get("category")
    if category is None:
        results.add_error("category_validation", "Missing __meta__.catalog.category")
        return

    if category not in VALID_CATEGORIES:
        results.add_error(
            "category_validation",
            f'Invalid category: "{category}". '
            f"Valid: {', '.join(sorted(VALID_CATEGORIES))}",
        )
        return

    results.add_passed(f"Category valid: {category}")

    if category == "Demos":
        multiuser = catalog.get("multiuser")
        if multiuser is True:
            results.add_error("category_validation", 'Category "Demos" must not be multi-user')

        redirect = catalog.get("workshopLabUiRedirect")
        if redirect is True:
            results.add_error(
                "category_validation",
                'Category "Demos" must not have workshopLabUiRedirect enabled',
            )


def check_dir_name_length(catalog_path: str, results: Results):
    """Check 24: catalog directory name must be <= 50 characters."""
    slug = os.path.basename(os.path.normpath(catalog_path))
    length = len(slug)
    if length > 50:
        results.add_error(
            "catalog_name_length",
            f"Catalog directory name too long ({length} chars) — maximum is 50",
        )
    else:
        results.add_passed(f"Catalog directory name length OK ({length}/50 chars): {slug}")


def check_best_practices(data: dict, results: Results):
    """Check 9: display_name length, keyword count/quality, owner defined."""
    meta = data.get("__meta__")
    if not isinstance(meta, dict):
        return

    catalog = meta.get("catalog")
    if not isinstance(catalog, dict):
        return

    display_name = catalog.get("display_name", "")
    if display_name:
        dn_len = len(display_name)
        if dn_len > 60:
            results.add_warning(
                "best_practices",
                f"Display name too long ({dn_len} chars) — must be 60 characters or fewer",
            )
        else:
            results.add_passed(f"Display name length OK ({dn_len} chars)")
    else:
        results.add_warning("best_practices", "No display_name defined")

    keywords = catalog.get("keywords", [])
    if not keywords:
        results.add_suggestion("best_practices", "No keywords defined")
    else:
        if len(keywords) > 4:
            results.add_suggestion(
                "best_practices",
                f"Too many keywords ({len(keywords)}) — keep to 3-4 meaningful terms",
            )
        generic_found = [kw for kw in keywords if str(kw).lower() in GENERIC_KEYWORDS]
        if generic_found:
            results.add_suggestion(
                "best_practices",
                f"Keywords contain generic terms that add no value: {generic_found}",
            )

    owners = meta.get("owners")
    if not owners:
        results.add_suggestion("best_practices", "No maintainer/owner defined")
    else:
        results.add_passed("Owners/maintainer defined")


def _is_credential_key(key: str) -> bool:
    """Return True if key looks like a credential variable."""
    lower = key.lower()
    for suffix in SKIP_SUFFIXES:
        if lower.endswith(suffix):
            return False
    return any(cred in lower for cred in CREDENTIAL_KEYWORDS)


def _is_vault_value(value) -> bool:
    """Return True if value is an ansible-vault encrypted string."""
    if not isinstance(value, str):
        return False
    return "$ANSIBLE_VAULT" in value


def _walk_flat(data: dict, prefix: str = "") -> list[tuple[str, object]]:
    """Yield (dotted_key, value) for all leaf nodes, recursing into lists."""
    items = []
    for k, v in data.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            items.extend(_walk_flat(v, full_key))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    items.extend(_walk_flat(item, f"{full_key}[{i}]"))
        else:
            items.append((full_key, v))
    return items


def check_password_pattern(data: dict, results: Results):
    """Check 19: no hardcoded or hash-based passwords."""
    cred_vars = []
    for key, value in _walk_flat(data):
        leaf = key.split(".")[-1]
        if not _is_credential_key(leaf):
            continue
        cred_vars.append((key, value))

    if not cred_vars:
        results.add_passed("No credential variables found (nothing to check)")
        return

    lookup_paths: dict[str, str] = {}
    all_ok = True

    for key, value in cred_vars:
        if _is_vault_value(value):
            continue

        val_str = str(value) if value is not None else ""

        if not val_str or val_str in ('""', "''", ""):
            continue

        uses_lookup = "lookup('password'" in val_str or 'lookup("password"' in val_str
        has_jinja = "{{" in val_str

        for pat in HASH_BAD_PATTERNS:
            if pat.search(val_str) and not uses_lookup:
                results.add_error(
                    "password_pattern",
                    f"{key} uses hash/GUID-based generation — not allowed",
                )
                all_ok = False
                break
        else:
            if not has_jinja and not uses_lookup and val_str.strip():
                results.add_error(
                    "password_pattern",
                    f"{key} is a hardcoded static password — not allowed",
                )
                all_ok = False

        if uses_lookup:
            match = re.search(r"output_dir\s*~\s*['\"]([^'\"]+)['\"]", val_str)
            if match:
                path = match.group(1)
                if path in lookup_paths:
                    results.add_error(
                        "password_pattern",
                        f"Duplicate lookup path '{path}' used by both {lookup_paths[path]} and {key}",
                    )
                    all_ok = False
                else:
                    lookup_paths[path] = key

    if all_ok:
        results.add_passed(
            f"All {len(cred_vars)} password variable(s) use safe patterns"
        )


def check_ee_image_date(data: dict, results: Results):
    """Check 21: execution environment image should not be > 90 days old."""
    meta = data.get("__meta__")
    if not isinstance(meta, dict):
        return

    deployer = meta.get("deployer")
    if not isinstance(deployer, dict):
        return

    ee = deployer.get("execution_environment")
    if not isinstance(ee, dict):
        return

    image = ee.get("image", "")
    m = EE_DATE_RE.search(str(image))
    if not m:
        return  # tag doesn't match chained-YYYY-MM-DD pattern; skip

    img_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
    age_days = (date.today() - img_date).days

    if age_days > 90:
        results.add_warning(
            "ee_image_date",
            f"Execution environment image is {age_days} days old (tag: {m.group(0)})",
        )
    else:
        results.add_passed(f"EE image date is recent: {m.group(1)} ({age_days} days old)")


def check_untagged_images(data: dict, catalog_path: str, results: Results):
    """Check 23: image references should use explicit pinned tags.

    Only runs when prod.yaml or event.yaml exists (per validator spec).
    """
    files = set(os.listdir(catalog_path))
    if "prod.yaml" not in files and "event.yaml" not in files:
        return

    unacceptable_tags = {"latest", "main", "master", "stable", "edge", "nightly"}
    image_re = re.compile(r"^[\w.\-]+(/[\w.\-]+){1,2}(:\S+)?$")

    issues = []
    for key, value in _walk_flat(data):
        if "image" not in key.lower().split(".")[-1]:
            continue
        val_str = str(value).strip()
        if "{{" in val_str:
            continue
        if not image_re.match(val_str):
            continue

        if ":" not in val_str:
            issues.append(f"{key} has no tag — images must be explicitly tagged")
        else:
            tag = val_str.rsplit(":", 1)[1]
            if tag.lower() in unacceptable_tags:
                issues.append(f"{key} uses tag ':{tag}' — not allowed")

    if issues:
        for issue in issues:
            results.add_error("untagged_images", issue)
    else:
        results.add_passed("All image references use explicit version tags")


def check_stage_files(catalog_path: str, results: Results):
    """Check 10: dev.yaml should exist; stage files should parse cleanly."""
    files = set(os.listdir(catalog_path))

    if "dev.yaml" not in files:
        results.add_warning("stage_files", "Missing dev.yaml")
    else:
        dev_path = os.path.join(catalog_path, "dev.yaml")
        try:
            with open(dev_path) as f:
                yaml.safe_load(f)
            results.add_passed("dev.yaml present and valid")
        except yaml.YAMLError:
            results.add_error("stage_files", "YAML syntax error in dev.yaml")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_checks(catalog_path: str) -> dict:
    """Run all 10 checks on the catalog at *catalog_path* and return results dict."""
    catalog_path = os.path.abspath(catalog_path)
    results = Results(catalog_path)

    if not os.path.isdir(catalog_path):
        results.add_error("file_structure", f"Catalog path is not a directory: {catalog_path}")
        return results.to_dict()

    # Check 24 — directory name length (independent of file contents)
    check_dir_name_length(catalog_path, results)

    # Check 1 — file structure
    has_common = check_file_structure(catalog_path, results)
    if not has_common:
        return results.to_dict()

    # Check 4 — YAML syntax
    data = check_yaml_syntax(catalog_path, results)
    if data is None:
        return results.to_dict()

    # Check 2 — UUID
    check_uuid(data, results)

    # Check 3 — category
    check_category(data, results)

    # Check 9 — best practices
    check_best_practices(data, results)

    # Check 19 — password patterns
    check_password_pattern(data, results)

    # Check 21 — EE image date
    check_ee_image_date(data, results)

    # Check 23 — untagged images (only for prod/event stages)
    check_untagged_images(data, catalog_path, results)

    # Check 10 — stage files
    check_stage_files(catalog_path, results)

    return results.to_dict()


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <catalog_directory> [--json]", file=sys.stderr)
        sys.exit(2)

    catalog_path = sys.argv[1]
    output_json = "--json" in sys.argv

    result = run_checks(catalog_path)

    if output_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"\nCatalog: {result['catalog_path']}")
        print(f"Status:  {result['status']}")
        print(f"Summary: {result['summary']}\n")

        if result["errors"]:
            print("ERRORS:")
            for e in result["errors"]:
                print(f"  [{e['check']}] {e['message']}")

        if result["warnings"]:
            print("WARNINGS:")
            for w in result["warnings"]:
                print(f"  [{w['check']}] {w['message']}")

        if result["suggestions"]:
            print("SUGGESTIONS:")
            for s in result["suggestions"]:
                print(f"  [{s['check']}] {s['message']}")

        if result["passed_checks"]:
            print("PASSED:")
            for p in result["passed_checks"]:
                print(f"  {p}")
        print()

    sys.exit(0 if result["status"] != "failed" else 1)


if __name__ == "__main__":
    main()
