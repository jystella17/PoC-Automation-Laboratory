from __future__ import annotations

import requests
import streamlit as st

DEFAULT_API_URL = "http://127.0.0.1:8000"

st.set_page_config(page_title="Supervisor Chat", page_icon="SC", layout="wide")
st.title("Supervisor Agent Chat")
st.caption("Basic chat interface connected to the FastAPI backend")

if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": "Enter infra/app requirements. You can also paste UserRequest JSON directly.",
        }
    ]

with st.sidebar:
    st.subheader("Connection")
    api_url = st.text_input("FastAPI URL", value=DEFAULT_API_URL)
    if st.button("Health Check"):
        try:
            resp = requests.get(f"{api_url}/health", timeout=5)
            st.success(resp.json())
        except Exception as exc:
            st.error(f"Connection failed: {exc}")

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

prompt = st.chat_input("Type your message")
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Generating response..."):
            try:
                payload = {"messages": st.session_state.messages}
                resp = requests.post(f"{api_url}/v1/chat", json=payload, timeout=30)
                resp.raise_for_status()
                reply = resp.json().get("reply", "Empty response")
            except Exception as exc:
                reply = f"Error: {exc}"

        st.markdown(reply)
        st.session_state.messages.append({"role": "assistant", "content": reply})

st.divider()
st.subheader("Supervisor Run Demo")
if st.button("Run Sample Request"):
    sample_payload = {
        "infra_tech_stack": {
            "os": "linux",
            "components": ["tomcat", "kafka"],
            "versions": {"tomcat": "10.x", "kafka": "3.5.x", "java": "17"},
        },
        "targets": [
            {
                "host": "10.0.0.10",
                "user": "ec2-user",
                "auth_ref": "secret://infra/key",
                "os_type": "ubuntu22.04",
            }
        ],
    }

    try:
        res = requests.post(f"{api_url}/v1/supervisor/run", json=sample_payload, timeout=30)
        st.json(res.json())
    except Exception as exc:
        st.error(f"Run failed: {exc}")
