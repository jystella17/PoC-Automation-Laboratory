from __future__ import annotations

VERSION_CATALOG: dict[str, list[str]] = {
    "apache": ["2.4.66", "2.4.65"],
    "tomcat": ["10.1.36", "10.1.35", "9.0.95"],
    "kafka": ["3.6.2", "3.6.1", "3.5.2"],
    "java": ["21.0.4", "17.0.12"],
    "pinpoint": ["Pinpoint v3", "Pinpoint v2"],
}

COMPONENT_VERSION_OPTIONS: dict[str, list[str]] = {
    component: ["None", *versions] if component not in {"java"} else versions
    for component, versions in {
        "apache": ["2.4.66", "2.4.65"],
        "tomcat": ["10", "9"],
        "kafka": ["3.6", "3.5"],
        "pinpoint": ["Pinpoint v3", "Pinpoint v2"],
    }.items()
}
