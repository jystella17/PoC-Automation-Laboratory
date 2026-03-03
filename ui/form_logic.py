from __future__ import annotations

from typing import Any

COMPONENT_OPTIONS = ["Apache", "Tomcat", "Kafka", "Pinpoint", "Others"]
OS_OPTIONS = ["Linux", "Windows"]
TARGET_OS_OPTIONS = ["Ubuntu22.04", "Rhel9", "Amazon Linux2023", "Debian12"]
LANGUAGE_OPTIONS = ["Java21", "Java17", "Python3.12", "Python3.11", "프롬프트로 직접 입력"]
DATABASE_OPTIONS = ["None", "MySQL", "PostgreSQL", "MariaDB", "Redis", "MongoDB"]
FRAMEWORK_OPTIONS = ["None", "Spring Boot", "Spring", "FastAPI"]
FRAMEWORK_VERSION_OPTIONS = ["None", "Spring Boot 4.0", "Spring Boot 3.5", "Spring Boot 3.0", "Spring 2.7", "FastAPI 0.135.1"]
FRAMEWORK_DEFAULT_VERSION = {
    "None": "None",
    "Spring Boot": "Spring Boot 4.0",
    "Spring": "Spring 2.7",
    "FastAPI": "FastAPI 0.135.1",
}

COMPONENT_RULES = {
    "apache": {"version_key": "apache_version", "instance_key": "apache_instance", "none_value": "None"},
    "tomcat": {"version_key": "tomcat_version", "instance_key": "tomcat_instance", "none_value": "None"},
    "kafka": {"version_key": "kafka_version", "instance_key": "kafka_consumer_instance", "none_value": "None"},
    "pinpoint": {"version_key": "pinpoint_version", "instance_key": "pinpoint_agent_instance", "none_value": "None"},
}
COMPONENT_VERSION_OPTIONS = {
    "apache": ["None", "2.4.66", "2.4.65"],
    "tomcat": ["None", "10", "9"],
    "kafka": ["None", "3.6", "3.5"],
    "pinpoint": ["None", "Pinpoint v3", "Pinpoint v2"],
}
FRAMEWORK_RULE = {
    "framework_key": "framework",
    "version_key": "framework_version",
    "instance_key": "application_instance",
    "none_value": "None",
}
DATABASE_RULE = {
    "database_key": "database",
    "user_key": "db_user",
    "password_key": "db_pw",
    "none_value": "None",
}


def normalize_component_name(component: str) -> str:
    return component.strip().lower()


def derive_java_version(languages: str | list[str]) -> str:
    values = languages if isinstance(languages, list) else [languages]
    for item in values:
        normalized = item.strip().lower()
        if normalized.startswith("java"):
            return item.strip().replace("Java", "")
    return ""


def sanitize_component_fields(form_values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    sanitized = dict(form_values)
    notices: list[str] = []
    selected = {normalize_component_name(component) for component in sanitized["components"]}

    for component, rule in COMPONENT_RULES.items():
        version_key = rule["version_key"]
        instance_key = rule["instance_key"]
        none_value = rule["none_value"]
        current_version = str(sanitized[version_key]).strip()
        current_instance = int(sanitized[instance_key])

        if component not in selected and (current_version != none_value or current_instance != 0):
            sanitized[version_key] = none_value
            sanitized[instance_key] = 0
            notices.append(f"선택하지 않은 {component} 에 대한 버전과 인스턴스 수 요청은 무시됩니다.")

    return sanitized, notices


def sanitize_framework_fields(form_values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    sanitized = dict(form_values)
    notices: list[str] = []
    none_value = FRAMEWORK_RULE["none_value"]
    framework = str(sanitized[FRAMEWORK_RULE["framework_key"]]).strip()
    version = str(sanitized[FRAMEWORK_RULE["version_key"]]).strip()
    instances = int(sanitized[FRAMEWORK_RULE["instance_key"]])

    if framework == none_value and version != none_value:
        sanitized[FRAMEWORK_RULE["version_key"]] = none_value
        notices.append("백엔드 프레임워크가 `None`이므로 세부 버전 요청은 무시됩니다.")
    if framework == none_value and instances > 0:
        sanitized[FRAMEWORK_RULE["instance_key"]] = 0
        notices.append("백엔드 프레임워크가 `None`이므로 애플리케이션 인스턴스 수 요청은 무시됩니다.")

    return sanitized, notices


def sanitize_database_fields(form_values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    sanitized = dict(form_values)
    notices: list[str] = []
    database = str(sanitized[DATABASE_RULE["database_key"]]).strip()
    db_user = str(sanitized[DATABASE_RULE["user_key"]]).strip()
    db_pw = str(sanitized[DATABASE_RULE["password_key"]]).strip()
    none_value = DATABASE_RULE["none_value"]

    if database == none_value and (db_user or db_pw):
        sanitized[DATABASE_RULE["user_key"]] = ""
        sanitized[DATABASE_RULE["password_key"]] = ""
        notices.append("데이터베이스가 `None`이므로 DB 사용자/비밀번호 입력은 무시됩니다.")

    return sanitized, notices


def validate_selected_components(form_values: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    selected = {normalize_component_name(component) for component in form_values["components"]}

    for component in selected:
        rule = COMPONENT_RULES.get(component)
        if not rule:
            continue

        version = str(form_values[rule["version_key"]]).strip()
        instances = int(form_values[rule["instance_key"]])

        if version == rule["none_value"]:
            errors.append(f"{component}을(를) 선택했으면 버전을 `None`이 아닌 값으로 지정해야 합니다.")
        if instances <= 0:
            errors.append(f"{component}을(를) 선택했으면 인스턴스 수를 1 이상으로 입력해야 합니다.")

    return errors


def validate_framework_selection(form_values: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    framework = str(form_values[FRAMEWORK_RULE["framework_key"]]).strip()
    version = str(form_values[FRAMEWORK_RULE["version_key"]]).strip()
    instances = int(form_values[FRAMEWORK_RULE["instance_key"]])
    none_value = FRAMEWORK_RULE["none_value"]

    if framework != none_value and version == none_value:
        errors.append("백엔드 프레임워크를 선택했으면 세부 버전을 `None`이 아닌 값으로 지정해야 합니다.")
    if framework != none_value and instances <= 0:
        errors.append("백엔드 프레임워크를 선택했으면 애플리케이션 인스턴스 수를 1 이상으로 입력해야 합니다.")

    return errors


def validate_target_fields(form_values: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not str(form_values["host"]).strip():
        errors.append("대상 호스트를 입력해야 합니다.")
    if not str(form_values["auth_ref"]).strip():
        errors.append("인증 정보를 입력해야 합니다.")
    return errors


def validate_database_selection(form_values: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    database = str(form_values[DATABASE_RULE["database_key"]]).strip()
    db_user = str(form_values[DATABASE_RULE["user_key"]]).strip()
    db_pw = str(form_values[DATABASE_RULE["password_key"]]).strip()
    none_value = DATABASE_RULE["none_value"]

    if database != none_value and (not db_user or not db_pw):
        errors.append("데이터베이스를 선택했으면 DB 사용자와 비밀번호를 모두 입력해야 합니다.")

    return errors


def apply_form_rules(form_values: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    notices: list[str] = []
    sanitized, component_notices = sanitize_component_fields(form_values)
    notices.extend(component_notices)

    sanitized, framework_notices = sanitize_framework_fields(sanitized)
    notices.extend(framework_notices)

    sanitized, database_notices = sanitize_database_fields(sanitized)
    notices.extend(database_notices)

    errors = validate_selected_components(sanitized)
    errors.extend(validate_framework_selection(sanitized))
    errors.extend(validate_target_fields(sanitized))
    errors.extend(validate_database_selection(sanitized))

    return sanitized, notices, errors


def build_user_request(form_values: dict[str, Any]) -> dict[str, Any]:
    normalized_components = [normalize_component_name(component) for component in form_values["components"]]
    java_version = derive_java_version(form_values["language"])

    return {
        "infra_tech_stack": {
            "os": form_values["os"],
            "components": normalized_components,
            "versions": {
                "apache": form_values["apache_version"],
                "tomcat": form_values["tomcat_version"],
                "kafka": form_values["kafka_version"],
                "pinpoint": form_values["pinpoint_version"],
                "java": java_version,
            },
            "instances": {
                "apache": form_values["apache_instance"],
                "tomcat": form_values["tomcat_instance"],
                "kafka_consumer": form_values["kafka_consumer_instance"],
                "pinpoint_agent": form_values["pinpoint_agent_instance"],
            },
        },
        "load_profile": {
            "tps": form_values["tps"],
            "payload_bytes": form_values["payload_bytes"],
            "duration_sec": form_values["duration_sec"],
            "concurrency": form_values["concurrency"],
        },
        "topology": {
            "nodes": form_values["nodes"],
            "apps": form_values["application_instance"],
        },
        "constraints": {
            "no_public_upload": form_values["no_public_upload"],
            "security_policy_notes": [],
            "sudo_allowed": form_values["sudo_allowed"],
        },
        "targets": [
            {
                "host": form_values["host"],
                "user": form_values["target_user"],
                "auth_ref": form_values["auth_ref"],
                "os_type": form_values["target_os_type"],
            }
        ],
        "logging": {
            "base_dir": form_values["base_dir"],
            "gc_log_dir": form_values["gc_log_dir"],
            "app_log_dir": form_values["app_log_dir"],
        },
        "app_tech_stack": {
            "framework": form_values["framework"],
            "minor_version": form_values["framework_version"],
            "language": form_values["language"],
            "databases": form_values["database"],
            "db_user": form_values["db_user"],
            "db_pw": form_values["db_pw"],
        },
        "additional_request": str(form_values["freeform_request"]).strip(),
    }
