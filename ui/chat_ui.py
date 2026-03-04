from __future__ import annotations

import json

import streamlit as st

from api_client import DEFAULT_API_URL, fetch_plan, health_check, request_chat_reply
from form_logic import (
    COMPONENT_OPTIONS,
    COMPONENT_RULES,
    COMPONENT_VERSION_OPTIONS,
    DATABASE_OPTIONS,
    FRAMEWORK_DEFAULT_VERSION,
    FRAMEWORK_OPTIONS,
    FRAMEWORK_VERSION_OPTIONS,
    LANGUAGE_OPTIONS,
    OS_OPTIONS,
    TARGET_OS_OPTIONS,
    apply_form_rules,
    build_user_request,
)


def init_messages() -> None:
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


def init_form_state() -> None:
    defaults: dict[str, object] = {
        "components": [],
        "apache_version": "None",
        "apache_instance": 0,
        "tomcat_version": "None",
        "tomcat_instance": 0,
        "kafka_version": "None",
        "kafka_consumer_instance": 0,
        "pinpoint_version": "None",
        "pinpoint_agent_instance": 0,
        "framework": "None",
        "framework_version": "None",
        "application_instance": 0,
        "language": [],
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    if "plan_cache_key" not in st.session_state:
        st.session_state.plan_cache_key = ""
    if "plan_cache_data" not in st.session_state:
        st.session_state.plan_cache_data = None
    if "plan_cache_error" not in st.session_state:
        st.session_state.plan_cache_error = None


def render_sidebar() -> str:
    with st.sidebar:
        st.subheader("연결 설정")
        api_url = st.text_input("FastAPI 주소", value=DEFAULT_API_URL)
        if st.button("헬스 체크"):
            try:
                st.success(health_check(api_url))
            except Exception as exc:
                st.error(f"연결 실패: {exc}")
    return api_url


def apply_component_defaults() -> None:
    selected_components = st.session_state.get("components", [])
    selected_normalized = {component.strip().lower() for component in selected_components}

    for component_name, rule in COMPONENT_RULES.items():
        options = COMPONENT_VERSION_OPTIONS.get(component_name)
        if not options:
            continue

        version_key = rule["version_key"]
        instance_key = rule["instance_key"]
        none_value = rule["none_value"]

        if component_name not in selected_normalized:
            st.session_state[version_key] = none_value
            st.session_state[instance_key] = 0

    for component in selected_components:
        normalized = component.strip().lower()
        rule = COMPONENT_RULES.get(normalized)
        options = COMPONENT_VERSION_OPTIONS.get(normalized)
        if not rule or not options or len(options) < 2:
            continue

        version_key = rule["version_key"]
        instance_key = rule["instance_key"]
        none_value = rule["none_value"]
        current_version = str(st.session_state.get(version_key, none_value)).strip()
        current_instance = int(st.session_state.get(instance_key, 0))

        # Only apply defaults when the component was effectively unconfigured.
        if current_version == none_value:
            st.session_state[version_key] = options[1]
        if current_instance == 0:
            st.session_state[instance_key] = 1


def apply_framework_defaults() -> None:
    framework = st.session_state.get("framework", "None")
    default_version = FRAMEWORK_DEFAULT_VERSION.get(framework, "None")

    if framework == "None":
        st.session_state["framework_version"] = "None"
        st.session_state["application_instance"] = 0
        return

    st.session_state["framework_version"] = default_version
    st.session_state["application_instance"] = 1


def render_form() -> tuple[bool, bool, dict[str, object]]:
    st.subheader("기본 서비스 스펙")

    infra_col, app_col = st.columns(2)

    with infra_col:
        os_name = st.selectbox("대상 서버 OS", OS_OPTIONS, index=0)
        target_os_type = st.selectbox("OS 버전", TARGET_OS_OPTIONS, index=0)
        components = st.multiselect(
            "인프라 기술스택",
            COMPONENT_OPTIONS,
            key="components",
            on_change=apply_component_defaults,
        )
        apache_version = st.selectbox(
            "Apache 버전",
            COMPONENT_VERSION_OPTIONS["apache"],
            key="apache_version",
        )
        apache_instance = st.number_input("Apache 인스턴스 수", min_value=0, step=1, key="apache_instance")
        tomcat_version = st.selectbox(
            "Tomcat 버전",
            COMPONENT_VERSION_OPTIONS["tomcat"],
            key="tomcat_version",
        )
        tomcat_instance = st.number_input("Tomcat 인스턴스 수", min_value=0, step=1, key="tomcat_instance")
        kafka_version = st.selectbox(
            "Kafka 버전",
            COMPONENT_VERSION_OPTIONS["kafka"],
            key="kafka_version",
        )
        kafka_consumer_instance = st.number_input(
            "Kafka Consumer 수",
            min_value=0,
            step=1,
            key="kafka_consumer_instance",
        )
        pinpoint_version = st.selectbox(
            "Pinpoint 버전",
            COMPONENT_VERSION_OPTIONS["pinpoint"],
            key="pinpoint_version",
        )
        pinpoint_agent_instance = st.number_input(
            "Pinpoint Agent 수 (애플리케이션 인스턴스 수와 동일)",
            min_value=0,
            step=1,
            key="pinpoint_agent_instance",
        )
        sudo_allowed = st.selectbox("sudo 정책", ["limited", "yes", "no"], index=0)
        no_public_upload = st.checkbox("외부 공개 업로드 차단", value=True)

    with app_col:
        framework = st.selectbox(
            "백엔드 프레임워크",
            FRAMEWORK_OPTIONS,
            key="framework",
            on_change=apply_framework_defaults,
        )
        framework_version = st.selectbox(
            "프레임워크 세부 버전",
            FRAMEWORK_VERSION_OPTIONS,
            key="framework_version",
        )
        application_instance = st.number_input(
            "애플리케이션 인스턴스 수",
            min_value=0,
            step=1,
            key="application_instance",
        )
        language = st.multiselect("프로그래밍 언어&버전", LANGUAGE_OPTIONS, key="language")
        database = st.selectbox("데이터베이스", DATABASE_OPTIONS, index=0)
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

    plan_check = st.button("Plan 점검")
    submit = st.button("테스트 환경 구축")

    return plan_check, submit, {
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
        "framework": st.session_state.get("framework", framework),
        "framework_version": st.session_state.get("framework_version", framework_version),
        "language": st.session_state.get("language", language),
        "database": database,
        "db_user": db_user,
        "db_pw": db_pw,
        "nodes": int(nodes),
        "application_instance": int(st.session_state.get("application_instance", application_instance)),
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


def describe_plan_step(step: dict[str, object]) -> str:
    name = str(step.get("name", "")).strip()
    status = str(step.get("status", "")).strip()
    detail = str(step.get("detail", "")).strip()

    if name == "plan":
        return "입력된 요구사항과 대상 환경 정보를 검토하고, 실제 작업을 시작할 수 있는 상태인지 먼저 확인합니다."
    if name == "build_infra":
        if status == "failed":
            return "필수 정보가 부족해 인프라 설치 및 환경 구성 작업은 아직 시작할 수 없습니다."
        return "대상 서버에 필요한 인프라 구성요소를 설치하고, 로그 경로와 기본 실행 환경을 준비합니다."
    if name == "generate_app":
        if status == "failed":
            return "필수 정보가 부족해 샘플 애플리케이션 생성 및 배포 준비 작업은 아직 시작할 수 없습니다."
        return "요청한 프레임워크와 언어 기준으로 샘플 애플리케이션을 만들고, 배포 가능한 산출물을 준비합니다."
    return detail


def render_plan_section(plan_data: dict[str, object] | None, plan_error: str | None) -> None:
    st.subheader("Agent 호출 Plan 점검")
    if plan_error:
        st.error(f"Plan 점검 실패: {plan_error}")
        return
    if plan_data is None:
        return

    st.write(plan_data.get("summary", ""))
    missing_requirements = plan_data.get("missing_requirements", [])
    if missing_requirements:
        st.warning("아래 필수 항목이 채워지기 전까지 실행이 차단됩니다.")
        for index, item in enumerate(missing_requirements, start=1):
            st.markdown(f"**질문 {index}.** {item['question']}")
            st.caption(item["reason"])
    else:
        st.success("필수 항목이 모두 채워졌습니다. 실행할 수 있습니다.")

    with st.expander("예상 Workflow", expanded=not bool(missing_requirements)):
        for step in plan_data.get("steps", []):
            st.markdown(f"- {describe_plan_step(step)}")

    graph = plan_data.get("graph", {})
    mermaid = graph.get("mermaid", "")
    if mermaid:
        with st.expander("LangGraph 시각화", expanded=False):
            st.code(mermaid, language="text")
            st.caption("Mermaid 지원 도구에 붙여 넣으면 node/edge 흐름을 시각화할 수 있습니다.")


def show_toasts(notice_list: list[str], error_list: list[str]) -> None:
    for notice in notice_list:
        st.toast(notice)
    for error in error_list:
        st.toast(error)

    if error_list:
        st.toast("입력한 기술 스택과 프레임워크 설정을 먼저 수정하세요.")


def handle_submit(submit: bool, api_url: str, payload: dict[str, object],
                  notice_list: list[str], error_list: list[str]) -> None:
    if not submit:
        return

    show_toasts(notice_list, error_list)
    if error_list:
        return

    message = json.dumps(payload, indent=2)
    st.session_state.messages.append({"role": "user", "content": message})
    try:
        reply = request_chat_reply(api_url, payload, st.session_state.messages[:-1])
    except Exception as exc:
        reply = f"Error: {exc}"
    st.session_state.messages.append({"role": "assistant", "content": reply})
    st.rerun()


def payload_cache_key(api_url: str, payload: dict[str, object]) -> str:
    return f"{api_url}::{json.dumps(payload, sort_keys=True, ensure_ascii=False)}"


def resolve_plan_data(
    plan_check: bool,
    submit: bool,
    api_url: str,
    payload: dict[str, object],
    validation_errors: list[str],
) -> tuple[dict[str, object] | None, str | None]:
    cache_key = payload_cache_key(api_url, payload)
    should_fetch = (plan_check or submit) and not validation_errors

    if should_fetch and st.session_state.plan_cache_key != cache_key:
        plan_data, plan_error = fetch_plan(api_url, payload)
        st.session_state.plan_cache_key = cache_key
        st.session_state.plan_cache_data = plan_data
        st.session_state.plan_cache_error = plan_error
    elif should_fetch and st.session_state.plan_cache_key == cache_key:
        plan_data = st.session_state.plan_cache_data
        plan_error = st.session_state.plan_cache_error
    else:
        plan_data = st.session_state.plan_cache_data if st.session_state.plan_cache_key == cache_key else None
        plan_error = st.session_state.plan_cache_error if st.session_state.plan_cache_key == cache_key else None

    return plan_data, plan_error


def render_messages() -> None:
    st.subheader("대화 내역")
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])


st.set_page_config(page_title="Supervisor Chat", page_icon="SC", layout="wide")
st.title("테스트 환경 자동 구성 Agent")
st.caption("인프라 테스트 환경 & 샘플 애플리케이션 자동 개발 Agent.")

init_messages()
init_form_state()
api_url = render_sidebar()
plan_checked, submitted, form_values = render_form()
sanitized_form_values, notices, validation_errors = apply_form_rules(form_values)
request_payload = build_user_request(sanitized_form_values)

plan_data, plan_error = resolve_plan_data(
    plan_check=plan_checked,
    submit=submitted,
    api_url=api_url,
    payload=request_payload,
    validation_errors=validation_errors,
)

st.subheader("테스트 환경 Spec")
st.code(json.dumps(request_payload, indent=2), language="json")
st.write("")

render_plan_section(plan_data, plan_error)
handle_submit(submitted, api_url, request_payload, notices, validation_errors)
render_messages()
st.divider()
