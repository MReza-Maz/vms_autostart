#!/usr/bin/env python3
import base64
import datetime as dt
import http.client
import json
import logging
import os
import ssl
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

CONFIG_FILE = os.environ.get("VMS_AUTOSTART_CONFIG", "/app/config.json")
DEFAULT_HTTP_TIMEOUT = 20
DEFAULT_SOAP_TIMEOUT = 30


def load_config(path=CONFIG_FILE):
    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)
    validate_config(config)
    return config


def load_password(path):
    with open(path, "r", encoding="utf-8") as f:
        password = f.read().rstrip("\r\n")
    if not password:
        raise RuntimeError(f"vCenter password file is empty: {path}")
    return password


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("Configuration root must be a JSON object")

    vcenter = config.get("vcenter")
    if not isinstance(vcenter, dict):
        raise ValueError("Missing vcenter configuration")
    for key in ("host", "username", "password_file"):
        if not str(vcenter.get(key, "")).strip():
            raise ValueError(f"Missing vcenter.{key}")
    port = int(vcenter.get("port", 443))
    if not 1 <= port <= 65535:
        raise ValueError("vcenter.port must be between 1 and 65535")

    startup = config.get("startup", {})
    if not isinstance(startup, dict):
        raise ValueError("startup must be an object")
    if startup.get("enabled", True) and not _string_list(startup.get("vms", [])):
        raise ValueError("startup.vms must contain at least one VM when startup is enabled")
    startup_timeout = int(startup.get("task_timeout", 300))
    if startup_timeout < 0:
        raise ValueError("startup.task_timeout cannot be negative")
    state_file = str(startup.get("state_file", "/app/state/startup.json"))
    if not os.path.isabs(state_file):
        raise ValueError("startup.state_file must be an absolute path")

    snapshots = config.get("snapshots", {})
    if not isinstance(snapshots, dict):
        raise ValueError("snapshots must be an object")
    if snapshots.get("enabled", True):
        if not _string_list(snapshots.get("vms", [])):
            raise ValueError("snapshots.vms must contain at least one VM when snapshots are enabled")
        prefix = str(snapshots.get("name_prefix", "auto-backup")).strip()
        if not prefix:
            raise ValueError("snapshots.name_prefix cannot be empty")
        # Count-based retention is the primary policy (keep newest N snapshots).
        # retention_days remains optional for age-based cleanup.
        if "retention_count" in snapshots:
            retention_count = int(snapshots.get("retention_count", 0))
        elif "retention_days" in snapshots:
            # Backward compatible: old configs only had retention_days.
            retention_count = 0
        else:
            retention_count = 10
        if retention_count < 0:
            raise ValueError("snapshots.retention_count cannot be negative")
        retention_days = int(snapshots.get("retention_days", 0))
        if retention_days < 0:
            raise ValueError("snapshots.retention_days cannot be negative")
        snapshot_timeout = int(snapshots.get("task_timeout", 600))
        if snapshot_timeout <= 0:
            raise ValueError("snapshots.task_timeout must be greater than zero")

    logging_cfg = config.get("logging", {})
    log_file = str(logging_cfg.get("log_file", "/var/log/vms_autostart/vms_autostart.log"))
    if not os.path.isabs(log_file):
        raise ValueError("logging.log_file must be an absolute path")


def _string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def setup_logging(config):
    path = config["logging"]["log_file"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    level = getattr(logging, config["logging"].get("level", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(path), logging.StreamHandler(sys.stdout)],
        force=True,
    )


class RestVCenter:
    def __init__(self, cfg, password):
        self.host = cfg["host"]
        self.port = int(cfg.get("port", 443))
        self.username = cfg["username"]
        self.password = password
        self.ignore_ssl = bool(cfg.get("ignore_ssl", True))
        self.timeout = int(cfg.get("http_timeout", DEFAULT_HTTP_TIMEOUT))
        self.retries = max(0, int(cfg.get("http_retries", 2)))
        self.session_id = None

    def _connection(self):
        if self.ignore_ssl:
            context = ssl._create_unverified_context()
        else:
            context = ssl.create_default_context()
        return http.client.HTTPSConnection(self.host, self.port, context=context, timeout=self.timeout)

    def request(self, method, path, body=None, headers=None):
        headers = dict(headers or {})
        headers.setdefault("User-Agent", "vms_autostart/2.1")
        if body is not None and isinstance(body, str):
            body = body.encode("utf-8")

        last_error = None
        for attempt in range(self.retries + 1):
            conn = self._connection()
            try:
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
                raw = response.read()
                content_type = response.getheader("Content-Type", "")
                if response.status >= 400:
                    text = raw.decode("utf-8", errors="replace")[:1000]
                    raise RuntimeError(
                        f"REST {method} {path} failed: HTTP {response.status}: {text}"
                    )
                return response.status, raw, content_type
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(1 + attempt)
                    continue
                raise RuntimeError(
                    f"REST connection failed for https://{self.host}:{self.port}{path}: {exc}"
                ) from exc
            finally:
                conn.close()
        raise RuntimeError(f"REST request failed: {last_error}")

    @staticmethod
    def decode_json(raw):
        if not raw:
            return None
        text = raw.decode("utf-8", errors="replace").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def login(self):
        credentials = f"{self.username}:{self.password}".encode("utf-8")
        token = base64.b64encode(credentials).decode("ascii")
        status, raw, _ = self.request(
            "POST",
            "/api/session",
            headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
        )
        # vSphere returns HTTP 201 for successful session creation.
        if status not in (200, 201) or not raw:
            raise RuntimeError(f"Unable to create vCenter REST session: HTTP {status}")

        value = self.decode_json(raw)
        if isinstance(value, str):
            session = value.strip()
        elif isinstance(value, dict):
            session = str(
                value.get("value")
                or value.get("session_id")
                or value.get("sessionId")
                or ""
            ).strip()
        else:
            session = ""

        if not session:
            raise RuntimeError("Unable to create vCenter REST session: invalid session response")

        self.session_id = session
        logging.info("Connected to vCenter using REST API.")

    def api(self, method, path, body=None):
        if not self.session_id:
            self.login()
        headers = {
            "Accept": "application/json",
            "vmware-api-session-id": self.session_id,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body, separators=(",", ":"))
        return self._api_once(method, path, body, headers)

    def _api_once(self, method, path, body, headers):
        status, raw, _ = self.request(method, path, body=body, headers=headers)
        if not raw:
            return status, None
        return status, self.decode_json(raw)

    def logout(self):
        if self.session_id:
            try:
                self.api("DELETE", "/api/session")
            except Exception:
                pass
            self.session_id = None

    def list_vms(self, names=None):
        path = "/api/vcenter/vm"
        if names:
            query = urllib.parse.urlencode([("names", name) for name in names])
            path += "?" + query
        _, data = self.api("GET", path)
        if not isinstance(data, list):
            raise RuntimeError("Unexpected response from vCenter VM list API")
        return data

    def find_vm(self, name):
        rows = self.list_vms([name])
        matches = [row for row in rows if row.get("name") == name]
        if not matches:
            return None
        if len(matches) > 1:
            raise RuntimeError(f"VM name is ambiguous in vCenter inventory: {name}")
        vm_id = matches[0].get("vm")
        if not vm_id:
            raise RuntimeError(f"vCenter returned no VM identifier for: {name}")
        return vm_id

    def power_state(self, vm_id):
        quoted = urllib.parse.quote(vm_id, safe="")
        _, data = self.api("GET", f"/api/vcenter/vm/{quoted}/power")
        if not isinstance(data, dict) or not data.get("state"):
            raise RuntimeError(f"Unexpected power-state response for VM {vm_id}")
        return data["state"]

    def power_on(self, vm_id):
        quoted = urllib.parse.quote(vm_id, safe="")
        path = f"/api/vcenter/vm/{quoted}/power?action=start"
        status, _ = self.api("POST", path)
        if status not in (200, 202, 204):
            raise RuntimeError(f"Unexpected PowerOn HTTP status for {vm_id}: {status}")

    def list_hosts(self):
        _, data = self.api("GET", "/api/vcenter/host")
        if not isinstance(data, list):
            raise RuntimeError("Unexpected response from vCenter host list API")
        return data


SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
VIM_NS = "urn:vim25"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
SOAP_ACTION = "urn:vim25/"


def qname(ns, name):
    return f"{{{ns}}}{name}"


class SoapVCenter:
    def __init__(self, cfg, password):
        self.host = cfg["host"]
        self.port = int(cfg.get("port", 443))
        self.username = cfg["username"]
        self.password = password
        self.ignore_ssl = bool(cfg.get("ignore_ssl", True))
        self.timeout = int(cfg.get("soap_timeout", DEFAULT_SOAP_TIMEOUT))
        self.retries = max(0, int(cfg.get("soap_retries", 2)))
        self.cookie = None

    def _request(self, body):
        last_error = None
        for attempt in range(self.retries + 1):
            context = ssl._create_unverified_context() if self.ignore_ssl else ssl.create_default_context()
            conn = http.client.HTTPSConnection(self.host, self.port, context=context, timeout=self.timeout)
            try:
                headers = {
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": SOAP_ACTION,
                    "User-Agent": "vms_autostart/2.1",
                }
                if self.cookie:
                    headers["Cookie"] = self.cookie
                conn.request("POST", "/sdk", body=body, headers=headers)
                response = conn.getresponse()
                raw = response.read()
                if response.status >= 400:
                    text = raw.decode("utf-8", errors="replace")[:1000]
                    raise RuntimeError(f"SOAP request failed: HTTP {response.status}: {text}")
                cookie = response.getheader("Set-Cookie")
                if cookie:
                    self.cookie = cookie.split(";", 1)[0]
                try:
                    root = ET.fromstring(raw)
                except ET.ParseError as exc:
                    raise RuntimeError("vCenter returned invalid SOAP XML") from exc
                fault = soap_fault(root)
                if fault:
                    raise RuntimeError(f"vCenter SOAP fault: {fault}")
                return root
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(1 + attempt)
                    continue
                raise RuntimeError(f"SOAP connection failed for https://{self.host}:{self.port}/sdk: {exc}") from exc
            finally:
                conn.close()
        raise RuntimeError(f"SOAP request failed: {last_error}")

    def _envelope(self, method_xml):
        return (
            f'<soapenv:Envelope xmlns:soapenv="{SOAP_ENV}" xmlns:vim="{VIM_NS}">'
            f"<soapenv:Body>{method_xml}</soapenv:Body></soapenv:Envelope>"
        ).encode("utf-8")

    def login(self):
        body = self._envelope(
            f'<vim:Login><vim:_this type="SessionManager">SessionManager</vim:_this>'
            f"<vim:userName>{xml_escape(self.username)}</vim:userName>"
            f"<vim:password>{xml_escape(self.password)}</vim:password></vim:Login>"
        )
        self._request(body)
        if not self.cookie:
            raise RuntimeError("vCenter SOAP login succeeded but no session cookie was returned")
        logging.info("Connected to vCenter using SOAP SDK.")

    def logout(self):
        if not self.cookie:
            return
        try:
            body = self._envelope(
                '<vim:Logout><vim:_this type="SessionManager">SessionManager</vim:_this></vim:Logout>'
            )
            self._request(body)
        except Exception:
            pass
        self.cookie = None

    def create_snapshot(self, vm_moref, name, description, memory, quiesce):
        if not self.cookie:
            self.login()
        body = self._envelope(
            f'<vim:CreateSnapshot_Task>'
            f'<vim:_this type="VirtualMachine">{xml_escape(vm_moref)}</vim:_this>'
            f"<vim:name>{xml_escape(name)}</vim:name>"
            f"<vim:description>{xml_escape(description)}</vim:description>"
            f"<vim:memory>{str(bool(memory)).lower()}</vim:memory>"
            f"<vim:quiesce>{str(bool(quiesce)).lower()}</vim:quiesce>"
            f"</vim:CreateSnapshot_Task>"
        )
        root = self._request(body)
        task = find_local(root, "returnval")
        if not task:
            raise RuntimeError("Snapshot task was not returned by vCenter")
        return task

    def list_snapshots(self, vm_moref):
        if not self.cookie:
            self.login()
        # vim25 SOAP serializes PropertyFilterSpec array items by placing
        # propSet/objectSet directly under specSet (no PropertyFilterSpec /
        # PropertySpec / ObjectSpec type wrappers). options is required for
        # RetrievePropertiesEx.
        method = (
            f'<vim:RetrievePropertiesEx>'
            f'<vim:_this type="PropertyCollector">propertyCollector</vim:_this>'
            f'<vim:specSet>'
            f'<vim:propSet>'
            f'<vim:type>VirtualMachine</vim:type>'
            f'<vim:all>false</vim:all>'
            f'<vim:pathSet>snapshot.rootSnapshotList</vim:pathSet>'
            f'</vim:propSet>'
            f'<vim:objectSet>'
            f'<vim:obj type="VirtualMachine">{xml_escape(vm_moref)}</vim:obj>'
            f'<vim:skip>false</vim:skip>'
            f'</vim:objectSet>'
            f'</vim:specSet>'
            f'<vim:options></vim:options>'
            f'</vim:RetrievePropertiesEx>'
        )
        root = self._request(self._envelope(method))
        snapshots = []
        for tree in root.iter():
            tag = local_name(tree.tag)
            # Real vCenter may emit SnapshotTree or VirtualMachineSnapshotTree.
            if tag not in ("SnapshotTree", "VirtualMachineSnapshotTree"):
                continue
            item = {}
            for child in list(tree):
                key = local_name(child.tag)
                if key in ("name", "createTime"):
                    if child.text:
                        item[key] = child.text
                elif key == "snapshot":
                    # moref may be element text and/or type attribute
                    if child.text and child.text.strip():
                        item["snapshot"] = child.text.strip()
                    elif child.get("xsi:type") or child.attrib:
                        # Some responses put the value only as text; keep attrib fallback empty
                        pass
            if item.get("snapshot"):
                snapshots.append(item)
        return snapshots

    def remove_snapshot(self, snapshot_moref):
        if not self.cookie:
            self.login()
        body = self._envelope(
            f'<vim:RemoveSnapshot_Task>'
            f'<vim:_this type="VirtualMachineSnapshot">{xml_escape(snapshot_moref)}</vim:_this>'
            f'<vim:removeChildren>false</vim:removeChildren>'
            f"</vim:RemoveSnapshot_Task>"
        )
        root = self._request(body)
        task = find_local(root, "returnval")
        if not task:
            raise RuntimeError("Snapshot removal task was not returned by vCenter")
        return task

    def _task_info(self, task_moref):
        """Return (state, error_message) for a Task managed object."""
        method = (
            f'<vim:RetrievePropertiesEx>'
            f'<vim:_this type="PropertyCollector">propertyCollector</vim:_this>'
            f'<vim:specSet>'
            f'<vim:propSet>'
            f'<vim:type>Task</vim:type>'
            f'<vim:all>false</vim:all>'
            f'<vim:pathSet>info.state</vim:pathSet>'
            f'<vim:pathSet>info.error</vim:pathSet>'
            f'</vim:propSet>'
            f'<vim:objectSet>'
            f'<vim:obj type="Task">{xml_escape(task_moref)}</vim:obj>'
            f'<vim:skip>false</vim:skip>'
            f'</vim:objectSet>'
            f'</vim:specSet>'
            f'<vim:options></vim:options>'
            f'</vim:RetrievePropertiesEx>'
        )
        root = self._request(self._envelope(method))
        state = None
        error_msg = None
        for propset in root.iter():
            if local_name(propset.tag) != "propSet":
                continue
            name = next((c.text for c in list(propset) if local_name(c.tag) == "name"), None)
            if name == "info.state":
                state = next((c.text for c in list(propset) if local_name(c.tag) == "val"), None)
            elif name == "info.error":
                # LocalizedMethodFault: prefer localizedMessage, then fault message
                val = next((c for c in list(propset) if local_name(c.tag) == "val"), None)
                if val is not None:
                    error_msg = find_local(val, "localizedMessage") or find_local(val, "message")
                    if not error_msg:
                        # Fall back to any nested fault text
                        for elem in val.iter():
                            if local_name(elem.tag) in ("localizedMessage", "message", "faultstring") and elem.text:
                                error_msg = elem.text
                                break
        return state, error_msg

    def _task_state(self, task_moref):
        state, _ = self._task_info(task_moref)
        return state

    def wait_task(self, task_moref, timeout):
        deadline = time.time() + int(timeout)
        while time.time() < deadline:
            state, error_msg = self._task_info(task_moref)
            if state == "success":
                return
            if state == "error":
                detail = error_msg or "no error details from vCenter"
                raise RuntimeError(f"vCenter task failed: {task_moref}: {detail}")
            if state in (None, "queued", "running"):
                time.sleep(2)
                continue
            raise RuntimeError(f"Unknown vCenter task state for {task_moref}: {state}")
        raise TimeoutError(f"vCenter task timed out: {task_moref}")


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def find_local(root, name):
    for elem in root.iter():
        if local_name(elem.tag) == name and elem.text:
            return elem.text
    return None


def soap_fault(root):
    fault = next((e for e in root.iter() if local_name(e.tag) == "Fault"), None)
    if fault is None:
        return None
    fault_string = find_local(fault, "faultstring")
    return fault_string or "unknown SOAP fault"


def xml_escape(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def read_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid state file: {path}") from exc


def write_state(path, state):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def normalize_host_state(value):
    if value is None:
        return None
    normalized = str(value).strip().lower()
    mapping = {
        "connected": "connected",
        "disconnected": "disconnected",
        "notresponding": "notresponding",
        "not_responding": "notresponding",
        "not responding": "notresponding",
    }
    return mapping.get(normalized, normalized)


def power_on_vms(rest, names, timeout):
    failures = []
    for name in names:
        try:
            vm_id = rest.find_vm(name)
            if not vm_id:
                raise RuntimeError(f"VM not found: {name}")
            state = rest.power_state(vm_id)
            logging.info("VM %s | PowerState: %s", name, state)
            if state in ("POWERED_ON", "POWERED_ON_WITH_PENDING_TASK"):
                logging.info("VM %s is already powered on", name)
                continue
            if state not in ("POWERED_OFF", "SUSPENDED"):
                raise RuntimeError(f"VM {name} has unsupported power state: {state}")
            rest.power_on(vm_id)
            logging.info("PowerOn command sent for VM: %s", name)
            if timeout <= 0:
                continue
            deadline = time.time() + timeout
            while time.time() < deadline:
                new_state = rest.power_state(vm_id)
                if new_state == "POWERED_ON":
                    logging.info("VM %s powered ON successfully", name)
                    break
                time.sleep(2)
            else:
                raise TimeoutError(f"PowerOn timed out for {name}")
        except Exception as exc:
            failures.append(name)
            logging.exception("Error processing VM %s: %s", name, exc)
    return failures


def run_autostart(config):
    vcfg = config["vcenter"]
    startup = config.get("startup", {})
    if not startup.get("enabled", True):
        logging.info("Auto-start is disabled")
        return

    password = load_password(vcfg["password_file"])
    rest = RestVCenter(vcfg, password)
    try:
        names = startup.get("vms", config.get("vms", []))
        rest.login()
        state_file = startup.get("state_file", "/app/state/startup.json")
        state = read_state(state_file)
        previous_hosts = state.get("hosts", {}) if isinstance(state.get("hosts", {}), dict) else {}

        configured_hosts = set(startup.get("hosts", []))
        host_rows = rest.list_hosts()
        if configured_hosts:
            host_rows = [
                row for row in host_rows
                if row.get("host") in configured_hosts or row.get("name") in configured_hosts
            ]
            if not host_rows:
                logging.warning("None of the configured ESXi hosts were found in vCenter")

        current = {}
        trigger = False
        initialized_before = bool(state.get("initialized"))

        for row in host_rows:
            host_ref = row.get("host") or row.get("name")
            if not host_ref:
                logging.warning("Ignoring ESXi host record without identifier: %s", row)
                continue
            host_state = normalize_host_state(row.get("connection_state"))
            current[host_ref] = host_state
            previous = normalize_host_state(previous_hosts.get(host_ref))
            logging.info("Host %s | Previous: %s | Current: %s", host_ref, previous, host_state)
            if host_state == "connected" and previous in ("disconnected", "notresponding"):
                trigger = True

        # On the first run, store a baseline only. This prevents an accidental
        # power-on of every configured VM simply because the service was installed
        # while ESXi was already running.
        if not initialized_before:
            trigger = bool(startup.get("trigger_on_first_run", False))
            if trigger:
                logging.info("First run trigger is enabled")
            else:
                logging.info("First run: ESXi baseline recorded; no PowerOn command sent")
        elif trigger:
            logging.info("ESXi connection transition detected")

        failures = power_on_vms(rest, names, int(startup.get("task_timeout", 300))) if trigger else []
        if not trigger:
            logging.info("No ESXi startup transition detected")

        new_state = {
            "initialized": True,
            "hosts": current,
            "last_check": dt.datetime.now().astimezone().isoformat(),
        }
        write_state(state_file, new_state)

        if failures:
            raise RuntimeError("Auto-start failed for VM(s): " + ", ".join(failures))
    finally:
        rest.logout()


def parse_vcenter_time(value):
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def snapshot_retention(soap, rest, names, prefix, retention_count, timeout, retention_days=0):
    """Delete excess auto-backup snapshots per VM.

    Primary policy (retention_count > 0): keep only the newest N snapshots
    whose name starts with ``prefix``; delete the rest (oldest first).

    Optional policy (retention_days > 0): also delete matching snapshots
    older than that many days, even if still within the count limit.

    Either policy can be used alone. Both zero disables retention.
    """
    if retention_count < 0:
        raise ValueError("retention_count cannot be negative")
    if retention_days < 0:
        raise ValueError("retention_days cannot be negative")
    if retention_count == 0 and retention_days == 0:
        logging.info("Snapshot retention is disabled (retention_count=0 and retention_days=0)")
        return []

    cutoff = None
    if retention_days > 0:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)

    epoch = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    failures = []
    for vm_name in names:
        try:
            vm_id = rest.find_vm(vm_name)
            if not vm_id:
                raise RuntimeError(f"VM not found for retention: {vm_name}")
            snapshots = soap.list_snapshots(vm_id)

            matched = []
            for snap in snapshots:
                snap_name = snap.get("name") or ""
                if not snap_name.startswith(prefix):
                    continue
                if not snap.get("snapshot"):
                    continue
                created = parse_vcenter_time(snap.get("createTime"))
                matched.append({
                    "name": snap_name,
                    "snapshot": snap["snapshot"],
                    "created": created,
                })

            # Newest first; unknown createTime treated as oldest.
            matched.sort(key=lambda item: item["created"] or epoch, reverse=True)

            logging.info(
                "Retention scan on %s: found %d snapshot(s) with prefix %r (limit=%s): %s",
                vm_name,
                len(matched),
                prefix,
                retention_count if retention_count > 0 else "off",
                ", ".join(item["name"] for item in matched) or "(none)",
            )

            delete_refs = set()
            to_delete = []

            def mark(item):
                ref = item["snapshot"]
                if ref not in delete_refs:
                    delete_refs.add(ref)
                    to_delete.append(item)

            # Count-based: anything beyond the newest N is removed.
            if retention_count > 0:
                for item in matched[retention_count:]:
                    mark(item)

            # Age-based: remove anything older than cutoff.
            if cutoff is not None:
                for item in matched:
                    if item["created"] is not None and item["created"] < cutoff:
                        mark(item)

            # Delete oldest first.
            to_delete.sort(key=lambda item: item["created"] or epoch)

            for item in to_delete:
                if item["created"] is None:
                    logging.warning(
                        "Deleting snapshot %s on %s with invalid createTime",
                        item["name"],
                        vm_name,
                    )
                task = soap.remove_snapshot(item["snapshot"])
                if task:
                    soap.wait_task(task, timeout)
                logging.info("Deleted old snapshot %s from VM %s", item["name"], vm_name)

            logging.info(
                "Retention on %s: matched=%d kept=%d deleted=%d (retention_count=%s)",
                vm_name,
                len(matched),
                len(matched) - len(to_delete),
                len(to_delete),
                retention_count if retention_count > 0 else "off",
            )
        except Exception as exc:
            failures.append(vm_name)
            logging.exception("Snapshot retention failed for VM %s: %s", vm_name, exc)
    return failures


def run_snapshots(config):
    vcfg = config["vcenter"]
    password = load_password(vcfg["password_file"])
    rest = RestVCenter(vcfg, password)
    soap = SoapVCenter(vcfg, password)
    snap_cfg = config.get("snapshots", {})
    if not snap_cfg.get("enabled", True):
        logging.info("Snapshots are disabled")
        return

    names = snap_cfg.get("vms", config.get("vms", []))
    prefix = str(snap_cfg.get("name_prefix", "auto-backup"))
    description = snap_cfg.get("description", "Automatic snapshot")
    task_timeout = int(snap_cfg.get("task_timeout", 600))
    if "retention_count" in snap_cfg:
        retention_count = int(snap_cfg.get("retention_count", 0))
    elif "retention_days" in snap_cfg and "retention_count" not in snap_cfg:
        # Old config without retention_count: do not invent a count limit.
        retention_count = 0
    else:
        retention_count = 10
    retention_days = int(snap_cfg.get("retention_days", 0))

    failures = []
    try:
        rest.login()
        soap.login()
        logging.info(
            "Snapshot job settings: prefix=%r retention_count=%s retention_days=%s vms=%s",
            prefix,
            retention_count,
            retention_days,
            names,
        )
        # Use local wall-clock time for the human-readable snapshot name.
        # Retention age comparisons (if enabled) still use UTC against vCenter createTime.
        timestamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        snapshot_name = f"{prefix}-{timestamp}"

        for vm_name in names:
            try:
                vm_id = rest.find_vm(vm_name)
                if not vm_id:
                    raise RuntimeError(f"VM not found for snapshot: {vm_name}")
                task = soap.create_snapshot(
                    vm_id,
                    snapshot_name,
                    description,
                    snap_cfg.get("memory", False),
                    snap_cfg.get("quiesce", False),
                )
                if snap_cfg.get("wait_for_task", True):
                    soap.wait_task(task, task_timeout)
                logging.info("Snapshot created for VM: %s", vm_name)
            except Exception as exc:
                failures.append(vm_name)
                logging.exception("Snapshot failed for VM %s: %s", vm_name, exc)

        failures.extend(
            snapshot_retention(
                soap,
                rest,
                names,
                prefix,
                retention_count,
                task_timeout,
                retention_days=retention_days,
            )
        )
    finally:
        soap.logout()
        rest.logout()

    if failures:
        unique = list(dict.fromkeys(failures))
        raise RuntimeError("Snapshot operation failed for VM(s): " + ", ".join(unique))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "power"
    if mode not in ("power", "snapshot"):
        print("Usage: vms_autostart.py [power|snapshot]", file=sys.stderr)
        return 2
    try:
        config = load_config()
        setup_logging(config)
        logging.info("Starting mode: %s", mode)
        if mode == "power":
            run_autostart(config)
        else:
            run_snapshots(config)
        logging.info("Finished mode: %s", mode)
        return 0
    except Exception as exc:
        logging.exception("Job failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
