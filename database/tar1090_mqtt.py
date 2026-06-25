"""Shared MQTT helpers for tar1090 alerting (used by the alerter and the API's test
button). Wraps paho-mqtt so both a one-shot publish and a long-lived client are easy,
and builds Home Assistant MQTT-Discovery payloads. Importable name (underscores) so the
dash-named scripts in this dir can `import tar1090_mqtt`."""

import json
import ssl

try:
    import paho.mqtt.client as mqtt
except ImportError:                      # image without paho -> alerting just no-ops
    mqtt = None


def available():
    return mqtt is not None


def make_client(cfg, client_id):
    # paho 2.x requires a CallbackAPIVersion; 1.x doesn't have it. Support both.
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id, clean_session=True)
    except (AttributeError, TypeError):
        c = mqtt.Client(client_id=client_id, clean_session=True)
    if cfg.get("mqtt_username"):
        c.username_pw_set(cfg["mqtt_username"], cfg.get("mqtt_password") or None)
    if cfg.get("mqtt_tls"):
        c.tls_set(cert_reqs=ssl.CERT_NONE)   # lenient: accept self-signed (typical on a LAN)
        c.tls_insecure_set(True)
    return c


def _dumps(payload):
    return payload if isinstance(payload, (str, bytes)) else json.dumps(payload)


def publish_once(cfg, topic, payload, retain=False, timeout=6):
    """Connect, publish one message, disconnect. Returns (ok: bool, error: str|None)."""
    if not available():
        return False, "paho-mqtt is not installed in this image"
    if not cfg.get("mqtt_host"):
        return False, "no MQTT broker host configured"
    c = make_client(cfg, "tar1090-test")
    try:
        c.connect(cfg["mqtt_host"], int(cfg.get("mqtt_port") or 1883), keepalive=10)
        c.loop_start()
        info = c.publish(topic, _dumps(payload), qos=1, retain=retain)
        info.wait_for_publish(timeout)
        ok = info.is_published()
        c.loop_stop()
        c.disconnect()
        return (True, None) if ok else (False, "publish timed out (broker reachable but no PUBACK)")
    except Exception as e:                  # noqa: BLE001 -- surface any connect/auth error to the UI
        try:
            c.loop_stop()
            c.disconnect()
        except Exception:
            pass
        return False, str(e)


# --- Home Assistant MQTT Discovery -------------------------------------------------------
def discovery_topic(cfg, rule_id):
    prefix = cfg.get("discovery_prefix") or "homeassistant"
    return f"{prefix}/binary_sensor/tar1090_rule_{rule_id}/config"


def state_topic(cfg, rule_id):
    base = cfg.get("base_topic") or "tar1090"
    return f"{base}/rule/{rule_id}/state"


def attributes_topic(cfg, rule_id):
    base = cfg.get("base_topic") or "tar1090"
    return f"{base}/rule/{rule_id}/attributes"


def event_topic(cfg):
    base = cfg.get("base_topic") or "tar1090"
    return f"{base}/event"


def discovery_payload(cfg, rule_id, rule_name, hold_sec):
    """A binary_sensor that pulses ON when the rule fires (auto-expires as a safety net)."""
    return {
        "name": rule_name,
        "unique_id": f"tar1090_rule_{rule_id}",
        "state_topic": state_topic(cfg, rule_id),
        "json_attributes_topic": attributes_topic(cfg, rule_id),
        "payload_on": "ON",
        "payload_off": "OFF",
        "device_class": "occupancy",
        "expire_after": max(hold_sec * 3, 120),
        "device": {
            "identifiers": ["tar1090_alerts"],
            "name": "tar1090 aircraft alerts",
            "manufacturer": "tar1090",
            "model": "history/alerts",
        },
    }
