from __future__ import annotations

import json

import requests
import streamlit as st

DEFAULT_API_URL = "http://127.0.0.1:8000"
COMPONENT_RULES = {
    "apache": {"version_key": "apache_version", "instance_key": "apache_instance", "none_value": "None"},
    "tomcat": {"version_key": "tomcat_version", "instance_key": "tomcat_instance", "none_value": "None"},
    "kafka": {"version_key": "kafka_version", "instance_key": "kafka_consumer_instance", "none_value": "None"},
    "pinpoint": {"version_key": "pinpoint_version", "instance_key": "pinpoint_agent_instance", "none_value": "None"},
}


def normalize_component_name(component: str) -> str:
    return component.strip().lower()


def derive_java_version(language: str) -> str:
    normalized = language.strip().lower()
    if normalized.startswith("java"):
        return language.strip().replace("Java", "")
    return ""


def sanitize_component_fields(form_values: dict[str, object]) -> tuple[dict[str, object], list[str]]:
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


def validate_selected_components(form_values: dict[str, object]) -> list[str]:
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


def build_user_request(form_values: dict[str, object]) -> dict[str, object]:
    normalized_components = [normalize_component_name(component) for component in form_values["components"]]
    java_version = derive_java_version(str(form_values["language"]))

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


def submit_chat(api_url: str, payload: dict[str, object]) -> None:
    message = json.dumps(payload, indent=2)
    st.session_state.messages.append({"role": "user", "content": message})

    try:
        resp = requests.post(
            f"{api_url}/v1/chat",
            json={"messages": st.session_state.messages},
            timeout=60,
        )
        resp.raise_for_status()
        reply = resp.json().get("reply", "Empty response")
    except Exception as exc:
        reply = f"Error: {exc}"

    st.session_state.messages.append({"role": "assistant", "content": reply})


def fetch_plan(api_url: str, payload: dict[str, object]) -> tuple[dict[str, object] | None, str | None]:
    try:
        resp = requests.post(f"{api_url}/v1/supervisor/plan", json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json(), None
    except Exception as exc:
        return None, str(exc)


st.set_page_config(page_title="Supervisor Chat", page_icon="SC", layout="wide")
st.title("테스트 환경 자동 구성 Agent")
st.caption("인프라 테스트 환경 & 샘플 애플리케이션 자동 개발 Agent.")

if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": (
                "기본 인프라/앱 스펙을 선택하고, 추가 요청은 프롬프트로 입력한 뒤 "
                "생성된 요청을 백엔드로 전송하세요."
            ),
        }
    ]

with st.sidebar:
    st.subheader("연결 설정")
    api_url = st.text_input("FastAPI 주소", value=DEFAULT_API_URL)
    if st.button("헬스 체크"):
        try:
            resp = requests.get(f"{api_url}/health", timeout=5)
            st.success(resp.json())
        except Exception as exc:
            st.error(f"연결 실패: {exc}")

with st.form("request_form"):
    st.subheader("기본 서비스 스펙")

    infra_col, app_col = st.columns(2)

    with infra_col:
        os_name = st.selectbox("대상 서버 OS", ["Linux", "Windows"], index=0)
        target_os_type = st.selectbox(
            "OS 버전",
            ["Ubuntu22.04", "Rhel9", "Amazon Linux2023", "Debian12"],
            index=0,
        )
        components = st.multiselect(
            "인프라 기술스택",
            ["apache", "tomcat", "kafka", "pinpoint", "others"],
            key="components",
        )
        apache_version = st.selectbox("Apache 버전", ["2.4.66", "2.4.65", "None"], index=0, key="apache_version")
        apache_instance = st.number_input("Apache 인스턴스 수", min_value=0, value=0, step=1, key="apache_instance")
        tomcat_version = st.selectbox("Tomcat 버전", ["10", "9", "None"], index=0, key="tomcat_version")
        tomcat_instance = st.number_input("Tomcat 인스턴스 수", min_value=0, value=0, step=1, key="tomcat_instance")
        kafka_version = st.selectbox("Kafka 버전", ["3.6", "3.5", "None"], index=0, key="kafka_version")
        kafka_consumer_instance = st.number_input(
            "Kafka Consumer 수",
            min_value=0,
            value=0,
            step=1,
            key="kafka_consumer_instance",
        )
        pinpoint_version = st.selectbox("Pinpoint 버전", ["Pinpoint v3", "Pinpoint v2", "None"], key="pinpoint_version")
        pinpoint_agent_instance = st.number_input(
            "Pinpoint Agent 수 (애플리케이션 인스턴스 수와 동일)",
            min_value=0,
            value=0,
            step=1,
            key="pinpoint_agent_instance",
        )
        sudo_allowed = st.selectbox("sudo 정책", ["limited", "yes", "no"], index=0)
        no_public_upload = st.checkbox("외부 공개 업로드 차단", value=True)

    with app_col:
        framework = st.selectbox("백엔드 프레임워크", ["Spring Boot", "Spring", "FastAPI", "None"], index=0)
        framework_version = st.selectbox("프레임워크 세부 버전", ["Spring Boot 4.0", "Spring Boot 3.5", "Spring Boot 3.0",
                                                         "Spring 2.7", "FastAPI 0.135.1", "None"], index=0)
        application_instance = st.number_input("애플리케이션 인스턴스 수", min_value=0, step=1)
        language = st.selectbox("언어", ["Java21", "Java17", "Python3.12", "None"], index=0)
        database = st.selectbox("데이터베이스", ["MySQL", "PostgreSQL", "MariaDB", "Redis", "MongoDB", "None"], index=0)
        db_user = st.text_input("DB 사용자", value="")
        db_pw = st.text_input("DB 비밀번호", value="", type="password")
        nodes = st.number_input("구성 노드 수", min_value=1, value=1, step=1)

    st.subheader("대상 서버 및 부하")
    target_col, load_col = st.columns(2)

    with target_col:
        host = st.text_input("대상 호스트", value="")
        target_user = st.text_input("SSH 사용자", value="ec2-user")
        auth_ref = st.text_input("인증 정보 (.pem 경로/계정 정보 등)", value="")
        base_dir = st.text_input("기본 로그 경로", value="/var/log/infra-test-lab")
        gc_log_dir = st.text_input("GC 로그 경로", value="/var/log/infra-test-lab/gc")
        app_log_dir = st.text_input("애플리케이션 로그 경로", value="/var/log/infra-test-lab/app")

    with load_col:
        tps = st.number_input("TPS", min_value=0, value=5000, step=100)
        payload_bytes = st.number_input("Payload 바이트", min_value=0, value=1024, step=128)
        duration_sec = st.number_input("실행 시간(초)", min_value=0, value=600, step=60)
        concurrency = st.number_input("동시 실행 (Thread & Loop)", min_value=0, value=200, step=10)

    freeform_request = st.text_area(
        "추가 요청 사항(자유 입력)",
        placeholder=(
            "예시: Kafka 벤치마크를 먼저 수행하고, Spring Boot 샘플 앱을 배포하며, "
            "메모리 릭 재현용 엔드포인트도 포함해줘."
        ),
        height=140,
    )

    submitted = st.form_submit_button("구조화된 요청 전송")

form_values = {
    "os": os_name,
    "target_os_type": target_os_type,
    "components": st.session_state.get("components", components),
    "apache_version": st.session_state.get("apache_version", apache_version),
    "apache_instance": int(st.session_state.get("apache_instance", apache_instance)),
    "tomcat_version": st.session_state.get("tomcat_version", tomcat_version),
    "tomcat_instance": int(st.session_state.get("tomcat_instance", tomcat_instance)),
    "kafka_version": st.session_state.get("kafka_version", kafka_version),
    "kafka_consumer_instance": int(st.session_state.get("kafka_consumer_instance", kafka_consumer_instance)),
    "pinpoint_version": st.session_state.get("pinpoint_version", pinpoint_version),
    "pinpoint_agent_instance": int(st.session_state.get("pinpoint_agent_instance", pinpoint_agent_instance)),
    "sudo_allowed": sudo_allowed,
    "no_public_upload": no_public_upload,
    "framework": framework,
    "framework_version": framework_version,
    "language": language,
    "database": database,
    "db_user": db_user,
    "db_pw": db_pw,
    "nodes": int(nodes),
    "application_instance": int(application_instance),
    "host": host,
    "target_user": target_user,
    "auth_ref": auth_ref,
    "base_dir": base_dir,
    "gc_log_dir": gc_log_dir,
    "app_log_dir": app_log_dir,
    "tps": int(tps),
    "payload_bytes": int(payload_bytes),
    "duration_sec": int(duration_sec),
    "concurrency": int(concurrency),
    "freeform_request": freeform_request,
}

sanitized_form_values, reset_notices = sanitize_component_fields(form_values)
validation_errors = validate_selected_components(sanitized_form_values)
request_payload = build_user_request(sanitized_form_values)
plan_data, plan_error = fetch_plan(api_url, request_payload)

st.subheader("생성된 UserRequest Payload")
st.code(json.dumps(request_payload, indent=2), language="json")

st.subheader("플래너 점검")
for notice in reset_notices:
    st.info(notice)
    st.toast(notice)

for error in validation_errors:
    st.error(error)

if plan_error:
    st.error(f"플랜 점검 실패: {plan_error}")

elif plan_data is not None:
    st.write(plan_data.get("summary", ""))
    missing_requirements = plan_data.get("missing_requirements", [])
    if missing_requirements:
        st.warning("아래 필수 항목이 채워지기 전까지 실행이 차단됩니다.")
        for index, item in enumerate(missing_requirements, start=1):
            st.markdown(f"**질문 {index}.** {item['question']}")
            st.caption(f"필드: `{item['field']}`")
            st.caption(item["reason"])
    else:
        st.success("필수 항목이 모두 채워졌습니다. 실행할 수 있습니다.")

    with st.expander("예상 워크플로우", expanded=not bool(missing_requirements)):
        for step in plan_data.get("steps", []):
            st.markdown(f"- `{step['name']}` [{step['status']}] {step['detail']}")

    graph = plan_data.get("graph", {})
    mermaid = graph.get("mermaid", "")
    if mermaid:
        with st.expander("LangGraph 시각화", expanded=False):
            st.code(mermaid, language="text")
            st.caption("Mermaid 지원 도구에 붙여 넣으면 node/edge 흐름을 시각화할 수 있습니다.")

action_col, preview_col = st.columns([1, 1])
with action_col:
    if submitted:
        if validation_errors:
            for error in validation_errors:
                st.toast(error)
            st.toast("선택한 기술 스택의 버전과 인스턴스 수를 먼저 수정하세요.")
        else:
            submit_chat(api_url, request_payload)
            st.rerun()

with preview_col:
    if st.button("현재 스펙으로 실행"):
        if validation_errors:
            st.toast("선택한 기술 스택의 버전과 인스턴스 수를 먼저 수정하세요.")
        else:
            try:
                res = requests.post(f"{api_url}/v1/supervisor/run", json=request_payload, timeout=30)
                if res.status_code >= 400:
                    detail = res.json().get("detail", {})
                    st.error(detail.get("message", "실행 실패"))
                    for item in detail.get("missing_requirements", []):
                        st.markdown(f"- `{item['field']}`: {item['question']}")
                else:
                    st.json(res.json())
            except Exception as exc:
                st.error(f"실행 실패: {exc}")

st.subheader("대화 내역")
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

st.divider()
